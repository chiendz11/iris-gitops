from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from automation.contracts import load_contract, validate_contract


SOURCE_REPOSITORY = "chiendz11/iris-infrastructure"
REQUIRED_SERVICE_ACCOUNT_ROLES = {
    "argo_events",
    "external_secrets",
    "mlflow",
    "training",
}


def _replace(
    path: Path,
    pattern: str,
    replacement: str,
    *,
    count: int = 0,
) -> Path:
    original = path.read_text(encoding="utf-8")
    updated, matches = re.subn(
        pattern,
        lambda match: match.expand(replacement),
        original,
        count=count,
        flags=re.MULTILINE,
    )
    if matches == 0:
        raise RuntimeError(f"Expected platform-owned field was not found in {path}")
    path.write_text(updated, encoding="utf-8")
    return path


def validate_platform_contract(contract: dict[str, Any]) -> None:
    validate_contract(contract, "platform-contract-v1.schema.json")
    if contract["source_repository"] != SOURCE_REPOSITORY:
        raise ValueError(f"Platform reconciliation may only be produced by {SOURCE_REPOSITORY}")

    roles = contract["iam_roles"]["service_accounts"]
    missing_roles = sorted(REQUIRED_SERVICE_ACCOUNT_ROLES - set(roles))
    if missing_roles:
        raise ValueError("Platform contract is missing service-account roles: " + ", ".join(missing_roles))

    domain = contract["domain"]
    missing_domain = sorted(name for name, value in domain.items() if value is None)
    if missing_domain:
        raise ValueError(
            "This production desired state enables public KServe and requires domain fields: "
            + ", ".join(missing_domain)
        )
    if contract["iam_roles"]["external_dns"] is None:
        raise ValueError("Public production domain requires the external-dns IAM role")


def _write_platform_state(root: Path, contract: dict[str, Any]) -> Path:
    """Persist stable deployment constraints, excluding run-specific provenance."""
    target = root / "state/production-platform.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "contract_version": contract["contract_version"],
        "environment": contract["environment"],
        "aws_region": contract["aws_region"],
        "cluster": contract["cluster"],
        "ecr_repositories": contract["ecr_repositories"],
    }
    target.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def render_platform_contract(root: Path, contract: dict[str, Any]) -> list[Path]:
    validate_platform_contract(contract)
    roles = contract["iam_roles"]["service_accounts"]
    domain = contract["domain"]
    region = contract["aws_region"]
    changed: list[Path] = []

    lbc = root / "applications/platform-aws-load-balancer-controller.yaml"
    changed.extend(
        [
            _replace(lbc, r"^(\s*clusterName:)\s+.*$", rf"\g<1> {contract['cluster']['name']}"),
            _replace(lbc, r"^(\s*region:)\s+.*$", rf"\g<1> {region}"),
            _replace(lbc, r"^(\s*vpcId:)\s+.*$", rf"\g<1> {contract['cluster']['vpc_id']}"),
            _replace(
                lbc,
                r"^(\s*eks\.amazonaws\.com/role-arn:)\s+.*$",
                rf"\g<1> {contract['iam_roles']['aws_load_balancer_controller']}",
            ),
        ]
    )

    external_dns = root / "applications/platform-external-dns.yaml"
    changed.extend(
        [
            _replace(
                external_dns,
                r"^(\s*- )(?:REPLACE_PUBLIC_DOMAIN_NAME|[^\s]+)$",
                rf"\g<1>{domain['name']}",
                count=1,
            ),
            _replace(
                external_dns,
                r"^(\s*zone-id-filter:)\s+.*$",
                rf"\g<1> {domain['zone_id']}",
            ),
            _replace(
                external_dns,
                r"^(\s*eks\.amazonaws\.com/role-arn:)\s+.*$",
                rf"\g<1> {contract['iam_roles']['external_dns']}",
            ),
        ]
    )

    external_secrets = root / "applications/platform-external-secrets.yaml"
    changed.append(
        _replace(
            external_secrets,
            r"^(\s*eks\.amazonaws\.com/role-arn:)\s+.*$",
            rf"\g<1> {roles['external_secrets']}",
        )
    )
    secret_store = root / "platform/external-secrets-config/cluster-secret-store.yaml"
    changed.append(_replace(secret_store, r"^(\s*region:)\s+.*$", rf"\g<1> {region}"))

    knative = root / "platform/knative/kustomization.yaml"
    changed.extend(
        [
            _replace(
                knative,
                r"^(\s*service\.beta\.kubernetes\.io/aws-load-balancer-ssl-cert:)\s+.*$",
                rf"\g<1> {domain['certificate_arn']}",
            ),
            _replace(
                knative,
                r"^(\s*external-dns\.alpha\.kubernetes\.io/hostname:)\s+.*$",
                rf"\g<1> {domain['kserve_hostname']}",
            ),
        ]
    )
    domain_mapping = root / "environments/production/inference-service/domain-mapping.yaml"
    changed.append(_replace(domain_mapping, r"^(\s{2}name:)\s+.*$", rf"\g<1> {domain['kserve_hostname']}"))

    artifact_repository = root / "environments/production/data-pipeline/artifact-repository.yaml"
    changed.extend(
        [
            _replace(
                artifact_repository,
                r"^(\s*bucket:)\s+.*$",
                rf"\g<1> {contract['storage']['argo_artifact_bucket']}",
            ),
            _replace(artifact_repository, r"^(\s*region:)\s+.*$", rf"\g<1> {region}"),
        ]
    )

    eventsource = root / "environments/production/data-pipeline/eventsource.yaml"
    changed.extend(
        [
            _replace(
                eventsource,
                r"^(\s*eks\.amazonaws\.com/role-arn:)\s+.*$",
                rf"\g<1> {roles['argo_events']}",
            ),
            _replace(eventsource, r"^(\s*region:)\s+.*$", rf"\g<1> {region}"),
            _replace(
                eventsource,
                r"^(\s*queue:)\s+.*$",
                rf"\g<1> {contract['events']['dataset_queue_name']}",
            ),
        ]
    )

    training_rbac = root / "environments/production/data-pipeline/rbac.yaml"
    changed.append(
        _replace(
            training_rbac,
            r"^(\s*eks\.amazonaws\.com/role-arn:)\s+.*$",
            rf"\g<1> {roles['training']}",
        )
    )
    intent_secret = root / "environments/production/data-pipeline/external-secret.yaml"
    changed.append(
        _replace(
            intent_secret,
            r"^(\s*key:)\s+.*$",
            rf"\g<1> {contract['automation']['model_release_publisher_secret_arn']}",
        )
    )
    sensor = root / "environments/production/data-pipeline/sensor.yaml"
    changed.append(
        _replace(
            sensor,
            r'^(\s*dataTemplate: ")[^"\s]+(@sha256:\{\{.*)$',
            rf"\g<1>{contract['ecr_repositories']['training']['url']}\g<2>",
        )
    )
    workflow = root / "environments/production/data-pipeline/workflow-template.yaml"
    changed.append(
        _replace(
            workflow,
            r"^(\s*- \{name: dataset-bucket, value:)\s+[^}]+(\})$",
            rf"\g<1> {contract['storage']['dvc_bucket']}\g<2>",
        )
    )

    registry_kustomization = root / "environments/production/model-registry/kustomization.yaml"
    changed.extend(
        [
            _replace(
                registry_kustomization,
                r"^(\s*- POSTGRES_HOST=).*$",
                rf"\g<1>{contract['registry']['rds_endpoint']}",
            ),
            _replace(
                registry_kustomization,
                r"^(\s*- MLFLOW_ARTIFACT_BUCKET=).*$",
                rf"\g<1>{contract['storage']['mlflow_artifact_bucket']}",
            ),
        ]
    )
    registry_account = root / "environments/production/model-registry/service-account.yaml"
    changed.append(
        _replace(
            registry_account,
            r"^(\s*eks\.amazonaws\.com/role-arn:)\s+.*$",
            rf"\g<1> {roles['mlflow']}",
        )
    )
    registry_secret = root / "environments/production/model-registry/external-secret.yaml"
    changed.append(
        _replace(
            registry_secret,
            r"(remoteRef: \{key:)\s+[^,]+(, property: (?:username|password)\})",
            rf"\g<1> {contract['registry']['rds_master_secret_arn']}\g<2>",
        )
    )

    changed.append(_write_platform_state(root, contract))
    return sorted(set(changed))


def main() -> None:
    parser = argparse.ArgumentParser(description="Render platform-contract-v1 into GitOps")
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    contract = load_contract(args.contract)
    validate_platform_contract(contract)
    metadata: dict[str, Any] = {
        "change_id": contract["change_id"],
        "branch_id": re.sub(r"[^A-Za-z0-9._-]", "-", contract["change_id"]),
        "source_sha": contract["source_sha"],
    }
    if not args.validate_only:
        metadata["paths"] = [
            str(path) for path in render_platform_contract(args.root, contract)
        ]
    print(json.dumps(metadata, separators=(",", ":")))


if __name__ == "__main__":
    main()
