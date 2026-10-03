from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

from automation.workload_release.renderer import (
    render_workload_release,
    validate_workload_release,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ACCOUNT = "123456789012"
REGION = "ap-southeast-1"
ECR_ROOT = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/iris-mlops-prod"


def isolated_root(tmp_path: Path) -> Path:
    shutil.copytree(REPOSITORY_ROOT / "environments", tmp_path / "environments")
    shutil.copytree(REPOSITORY_ROOT / "contracts/workload-config", tmp_path / "contracts/workload-config")
    state = {
        "contract_version": "v1",
        "environment": "production",
        "aws_region": REGION,
        "cluster": {"name": "iris-prod", "vpc_id": "vpc-0123abcdef"},
        "ecr_repositories": {
            name: {"name": f"iris-mlops-prod/{name}", "url": f"{ECR_ROOT}/{name}"}
            for name in ("training", "inference", "mlflow", "dispatcher")
        },
    }
    (tmp_path / "state").mkdir()
    (tmp_path / "state/production-platform.json").write_text(json.dumps(state))
    return tmp_path


def schema_digest(root: Path, component: str, version: str = "v1") -> str:
    data = (root / f"contracts/workload-config/{component}-{version}.schema.json").read_bytes()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def inference_contract(root: Path) -> dict:
    return {
        "contract_version": "v1",
        "kind": "workload_release",
        "component": "inference",
        "environment": "production",
        "image": {"repository": f"{ECR_ROOT}/inference", "digest": "sha256:" + "a" * 64},
        "runtime_config": {
            "DRIFT_WINDOW_SIZE": 500,
            "DRIFT_MIN_SAMPLES": 50,
            "DRIFT_ZSCORE_THRESHOLD": 2.5,
        },
        "config_schema_version": "v1",
        "config_schema_digest": schema_digest(root, "inference"),
        "required_config": [
            "DRIFT_WINDOW_SIZE",
            "DRIFT_MIN_SAMPLES",
            "DRIFT_ZSCORE_THRESHOLD",
        ],
        "secret_refs": {},
        "source_repository": "chiendz11/iris-inference-service",
        "source_sha": "b" * 40,
        "change_id": "release-123",
    }


def test_atomic_image_and_config_release(tmp_path: Path) -> None:
    root = isolated_root(tmp_path)
    render_workload_release(root, inference_contract(root))
    manifest = yaml.safe_load(
        (root / "environments/production/inference-service/inferenceservice.yaml").read_text()
    )
    container = manifest["spec"]["predictor"]["containers"][0]
    assert container["image"] == f"{ECR_ROOT}/inference@sha256:" + "a" * 64
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["DRIFT_WINDOW_SIZE"] == "500"


def test_config_only_release_keeps_current_image(tmp_path: Path) -> None:
    root = isolated_root(tmp_path)
    payload = inference_contract(root)
    render_workload_release(root, payload)
    del payload["image"]
    before = yaml.safe_load(
        (root / "environments/production/inference-service/inferenceservice.yaml").read_text()
    )["spec"]["predictor"]["containers"][0]["image"]
    render_workload_release(root, payload)
    after = yaml.safe_load(
        (root / "environments/production/inference-service/inferenceservice.yaml").read_text()
    )["spec"]["predictor"]["containers"][0]["image"]
    assert after == before


def test_image_only_release_validates_and_keeps_current_config(tmp_path: Path) -> None:
    root = isolated_root(tmp_path)
    payload = inference_contract(root)
    before = yaml.safe_load(
        (root / "environments/production/inference-service/inferenceservice.yaml").read_text()
    )["spec"]["predictor"]["containers"][0]["env"]
    del payload["runtime_config"]
    del payload["secret_refs"]
    render_workload_release(root, payload)
    after_manifest = yaml.safe_load(
        (root / "environments/production/inference-service/inferenceservice.yaml").read_text()
    )
    container = after_manifest["spec"]["predictor"]["containers"][0]
    assert container["image"] == f"{ECR_ROOT}/inference@sha256:" + "a" * 64
    assert container["env"] == before


def test_image_only_rejects_incompatible_current_config(tmp_path: Path) -> None:
    root = isolated_root(tmp_path)
    manifest_path = root / "environments/production/inference-service/inferenceservice.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    container = manifest["spec"]["predictor"]["containers"][0]
    container["env"] = [
        item for item in container["env"] if item["name"] != "DRIFT_MIN_SAMPLES"
    ]
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    payload = inference_contract(root)
    del payload["runtime_config"]
    del payload["secret_refs"]
    with pytest.raises(ValueError, match="Current desired config"):
        validate_workload_release(root, payload)


def test_inference_release_is_blocked_during_model_canary(tmp_path: Path) -> None:
    root = isolated_root(tmp_path)
    manifest_path = root / "environments/production/inference-service/inferenceservice.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["spec"]["predictor"]["canaryTrafficPercent"] = 10
    manifest["metadata"].setdefault("annotations", {})[
        "mlops.iris/candidate-model-version"
    ] = "12"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    with pytest.raises(ValueError, match="model rollout is active"):
        validate_workload_release(root, inference_contract(root))


def test_rejects_required_config_that_disagrees_with_approved_schema(
    tmp_path: Path,
) -> None:
    root = isolated_root(tmp_path)
    payload = inference_contract(root)
    payload["required_config"] = ["DRIFT_WINDOW_SIZE"]
    with pytest.raises(ValueError, match="exactly match"):
        validate_workload_release(root, payload)


def test_config_only_is_rejected_before_initial_image(tmp_path: Path) -> None:
    root = isolated_root(tmp_path)
    payload = inference_contract(root)
    del payload["image"]
    with pytest.raises(ValueError, match="first inference release"):
        validate_workload_release(root, payload)


def test_secret_reference_creates_external_secret_without_secret_value(tmp_path: Path) -> None:
    root = isolated_root(tmp_path)
    payload = inference_contract(root)
    payload["secret_refs"] = {
        "UPSTREAM_API_TOKEN": {"key": "iris/inference/upstream", "property": "token"}
    }
    render_workload_release(root, payload)
    external_secret = yaml.safe_load(
        (root / "environments/production/inference-service/runtime-external-secret.yaml").read_text()
    )
    assert external_secret["spec"]["data"][0]["remoteRef"]["key"] == "iris/inference/upstream"
    assert "secret-value" not in json.dumps(external_secret)


def test_rejects_repository_outside_platform_allow_list(tmp_path: Path) -> None:
    root = isolated_root(tmp_path)
    payload = inference_contract(root)
    payload["image"]["repository"] = f"{ECR_ROOT}/another-image"
    with pytest.raises(ValueError, match="Terraform-approved"):
        validate_workload_release(root, payload)


def test_rejects_unapproved_schema_digest(tmp_path: Path) -> None:
    root = isolated_root(tmp_path)
    payload = inference_contract(root)
    payload["config_schema_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="GitOps-approved schema"):
        validate_workload_release(root, payload)


def test_accepts_a_separately_approved_v2_schema(tmp_path: Path) -> None:
    root = isolated_root(tmp_path)
    v1 = root / "contracts/workload-config/inference-v1.schema.json"
    v2 = root / "contracts/workload-config/inference-v2.schema.json"
    v2.write_bytes(v1.read_bytes())
    payload = inference_contract(root)
    payload["config_schema_version"] = "v2"
    payload["config_schema_digest"] = (
        "sha256:" + hashlib.sha256(v2.read_bytes()).hexdigest()
    )

    render_workload_release(root, payload)

    manifest = yaml.safe_load(
        (root / "environments/production/inference-service/inferenceservice.yaml").read_text()
    )
    assert manifest["spec"]["predictor"]["containers"][0]["image"].endswith(
        "@sha256:" + "a" * 64
    )


def test_rejects_plain_secret_like_config(tmp_path: Path) -> None:
    root = isolated_root(tmp_path)
    payload = inference_contract(root)
    payload["runtime_config"]["API_TOKEN"] = "do-not-commit"
    with pytest.raises(ValueError, match="Secret-like"):
        validate_workload_release(root, payload)


def test_model_registry_release_updates_digest_and_config(tmp_path: Path) -> None:
    root = isolated_root(tmp_path)
    payload = {
        "contract_version": "v1",
        "kind": "workload_release",
        "component": "model-registry",
        "environment": "production",
        "image": {
            "repository": f"{ECR_ROOT}/mlflow",
            "digest": "sha256:" + "c" * 64,
        },
        "runtime_config": {
            "MLFLOW_ALLOWED_HOSTS": "mlflow,mlflow.mlops.svc.cluster.local",
            "MLFLOW_CORS_ALLOWED_ORIGINS": "http://mlflow.mlops.svc.cluster.local:5000",
            "MLFLOW_WORKERS": 3,
        },
        "config_schema_version": "v1",
        "config_schema_digest": schema_digest(root, "model-registry"),
        "required_config": [
            "MLFLOW_ALLOWED_HOSTS",
            "MLFLOW_CORS_ALLOWED_ORIGINS",
            "MLFLOW_WORKERS",
        ],
        "source_repository": "chiendz11/iris-model-registry",
        "source_sha": "d" * 40,
        "change_id": "release-456",
    }
    render_workload_release(root, payload)
    desired = yaml.safe_load(
        (root / "environments/production/model-registry/kustomization.yaml").read_text()
    )
    assert desired["images"][0]["newName"] == f"{ECR_ROOT}/mlflow"
    assert desired["images"][0]["digest"] == "sha256:" + "c" * 64
    assert "newTag" not in desired["images"][0]
    assert "MLFLOW_WORKERS=3" in desired["configMapGenerator"][0]["literals"]


def test_model_registry_v2_disables_unused_job_execution(tmp_path: Path) -> None:
    root = isolated_root(tmp_path)
    payload = {
        "contract_version": "v1",
        "kind": "workload_release",
        "component": "model-registry",
        "environment": "production",
        "runtime_config": {
            "MLFLOW_ALLOWED_HOSTS": "mlflow,mlflow.mlops.svc.cluster.local",
            "MLFLOW_CORS_ALLOWED_ORIGINS": "http://mlflow.mlops.svc.cluster.local:5000",
            "MLFLOW_SERVER_ENABLE_JOB_EXECUTION": False,
            "MLFLOW_WORKERS": 1,
        },
        "config_schema_version": "v2",
        "config_schema_digest": schema_digest(root, "model-registry", "v2"),
        "required_config": [
            "MLFLOW_ALLOWED_HOSTS",
            "MLFLOW_CORS_ALLOWED_ORIGINS",
            "MLFLOW_SERVER_ENABLE_JOB_EXECUTION",
            "MLFLOW_WORKERS",
        ],
        "source_repository": "chiendz11/iris-model-registry",
        "source_sha": "e" * 40,
        "change_id": "config-only-v2",
    }

    render_workload_release(root, payload)

    desired = yaml.safe_load(
        (root / "environments/production/model-registry/kustomization.yaml").read_text()
    )
    literals = desired["configMapGenerator"][0]["literals"]
    assert "MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false" in literals
    assert "MLFLOW_WORKERS=1" in literals


def test_release_automation_supports_image_only(tmp_path: Path) -> None:
    root = isolated_root(tmp_path)
    payload = {
        "contract_version": "v1",
        "kind": "workload_release",
        "component": "release-automation",
        "environment": "production",
        "image": {
            "repository": f"{ECR_ROOT}/dispatcher",
            "digest": "sha256:" + "e" * 64,
        },
        "source_repository": "chiendz11/iris-gitops",
        "source_sha": "f" * 40,
        "change_id": "release-automation-1",
    }
    render_workload_release(root, payload)
    workflow = (root / "environments/production/data-pipeline/workflow-template.yaml").read_text()
    assert f"{ECR_ROOT}/dispatcher@sha256:" + "e" * 64 in workflow
