from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

from automation.platform_reconcile.renderer import (
    render_platform_contract,
    validate_platform_contract,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ACCOUNT = "123456789012"
REGION = "ap-southeast-1"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/iris-role"


def contract() -> dict:
    ecr_root = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/iris-mlops-prod"
    return {
        "contract_version": "v1",
        "producer": "iris-infrastructure",
        "environment": "production",
        "source_repository": "chiendz11/iris-infrastructure",
        "source_sha": "a" * 40,
        "change_id": "run-123-1",
        "aws_region": REGION,
        "cluster": {"name": "iris-mlops-prod", "vpc_id": "vpc-0123abcdef"},
        "storage": {
            "dvc_bucket": "iris-prod-dvc-12345678",
            "mlflow_artifact_bucket": "iris-prod-mlflow-12345678",
            "argo_artifact_bucket": "iris-prod-argo-12345678",
        },
        "events": {
            "dataset_queue_name": "iris-prod-dataset-events",
            "dataset_queue_url": f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/iris-prod-dataset-events",
            "dataset_queue_arn": f"arn:aws:sqs:{REGION}:{ACCOUNT}:iris-prod-dataset-events",
        },
        "registry": {
            "rds_endpoint": "iris.example.ap-southeast-1.rds.amazonaws.com",
            "rds_master_secret_arn": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:iris-rds-AbCdEf",
        },
        "ecr_repositories": {
            name: {"name": f"iris-mlops-prod/{name}", "url": f"{ecr_root}/{name}"}
            for name in ("training", "inference", "mlflow", "dispatcher")
        },
        "iam_roles": {
            "service_accounts": {
                "mlflow": ROLE,
                "training": ROLE,
                "argo_events": ROLE,
                "external_secrets": ROLE,
                "external_dns": ROLE,
            },
            "aws_load_balancer_controller": ROLE,
            "external_dns": ROLE,
        },
        "automation": {
            "model_release_publisher_secret_arn": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:iris-model-release-AbCdEf",
        },
        "domain": {
            "name": "example.com",
            "kserve_hostname": "api.example.com",
            "zone_id": "Z0123456789ABC",
            "certificate_arn": f"arn:aws:acm:{REGION}:{ACCOUNT}:certificate/1234",
            "public_url": "https://api.example.com",
        },
    }


def isolated_root(tmp_path: Path) -> Path:
    for directory in ("applications", "platform", "environments"):
        shutil.copytree(REPOSITORY_ROOT / directory, tmp_path / directory)
    return tmp_path


def test_renders_platform_fields_and_stable_image_allow_list(tmp_path: Path) -> None:
    root = isolated_root(tmp_path)
    payload = contract()
    render_platform_contract(root, payload)
    render_platform_contract(root, payload)

    lbc = yaml.safe_load(
        (root / "applications/platform-aws-load-balancer-controller.yaml").read_text()
    )
    values = lbc["spec"]["source"]["helm"]["valuesObject"]
    assert values["clusterName"] == "iris-mlops-prod"
    assert values["vpcId"] == "vpc-0123abcdef"

    sensor = (root / "environments/production/data-pipeline/sensor.yaml").read_text()
    assert payload["ecr_repositories"]["training"]["url"] in sensor
    secret = (root / "environments/production/data-pipeline/external-secret.yaml").read_text()
    assert payload["automation"]["model_release_publisher_secret_arn"] in secret

    state = json.loads((root / "state/production-platform.json").read_text())
    assert state["ecr_repositories"]["inference"] == payload["ecr_repositories"]["inference"]
    assert "source_sha" not in state

    unresolved = set()
    for directory in ("applications", "platform", "environments"):
        for path in (root / directory).rglob("*.yaml"):
            if "examples" in path.parts:
                continue
            unresolved.update(re.findall(r"REPLACE_[A-Z0-9_]+", path.read_text()))
    assert unresolved == {
        "REPLACE_ECR_INFERENCE_IMAGE",
        "REPLACE_ECR_MLFLOW_REPOSITORY",
        "REPLACE_IMAGE_TAG",
    }


def test_rejects_untrusted_producer() -> None:
    payload = contract()
    payload["source_repository"] = "someone/another-repo"
    with pytest.raises(ValueError):
        validate_platform_contract(payload)


def test_rejects_receiver_credentials_in_contract() -> None:
    payload = contract()
    payload["iam_roles"]["gitops_automation"] = ROLE
    payload["automation"]["gitops_automation_secret_arn"] = (
        f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:iris-gitops-AbCdEf"
    )
    with pytest.raises(ValueError):
        validate_platform_contract(payload)


def test_public_production_requires_complete_domain() -> None:
    payload = contract()
    payload["domain"]["certificate_arn"] = None
    with pytest.raises(ValueError, match="certificate_arn"):
        validate_platform_contract(payload)
