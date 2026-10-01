from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from automation.contracts import load_contract, validate_contract


COMPONENTS = {
    "inference": {
        "source_repository": "chiendz11/iris-inference-service",
        "target": "environments/production/inference-service/inferenceservice.yaml",
        "kustomization": "environments/production/inference-service/kustomization.yaml",
        "config_schema": "contracts/workload-config/inference-{version}.schema.json",
        "secret_name": "inference-runtime",
    },
    "model-registry": {
        "source_repository": "chiendz11/iris-model-registry",
        "target": "environments/production/model-registry/kustomization.yaml",
        "deployment": "environments/production/model-registry/deployment.yaml",
        "kustomization": "environments/production/model-registry/kustomization.yaml",
        "config_schema": "contracts/workload-config/model-registry-{version}.schema.json",
        "secret_name": "model-registry-runtime",
    },
    "release-automation": {
        "source_repository": "chiendz11/iris-gitops",
        "target": "environments/production/data-pipeline/workflow-template.yaml",
    },
}
SENSITIVE_NAME = re.compile(
    r"(?:PASSWORD|PRIVATE|SECRET|TOKEN|CREDENTIAL|ACCESS_KEY|API_KEY)", re.IGNORECASE
)
PLATFORM_ECR_KEYS = {
    "inference": "inference",
    "model-registry": "mlflow",
    "release-automation": "dispatcher",
}


def _string_value(value: str | int | float | bool) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _approved_config_schema(root: Path, contract: dict[str, Any]) -> Path:
    component = contract["component"]
    template = COMPONENTS[component].get("config_schema")
    if template is None:
        raise ValueError(f"{component} does not accept runtime configuration")
    return root / template.format(version=contract["config_schema_version"])


def _coerce_desired_value(value: str, schema: dict[str, Any]) -> Any:
    """Recover JSON types from string-only Kubernetes env/configMap values."""
    expected_type = schema.get("type")
    try:
        if expected_type == "integer":
            return int(value)
        if expected_type == "number":
            return float(value)
        if expected_type == "boolean":
            normalized = value.lower()
            if normalized not in {"true", "false"}:
                raise ValueError
            return normalized == "true"
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Current desired value {value!r} cannot be interpreted as {expected_type}"
        ) from error
    return value


def _current_runtime_config(
    root: Path, component: str, schema: dict[str, Any]
) -> dict[str, Any]:
    properties = schema.get("properties", {})
    if component == "inference":
        manifest = yaml.safe_load((root / COMPONENTS[component]["target"]).read_text())
        entries = manifest["spec"]["predictor"]["containers"][0].get("env", [])
        desired = {
            item["name"]: item["value"]
            for item in entries
            if item.get("name") in properties and "value" in item
        }
    else:
        manifest = yaml.safe_load((root / COMPONENTS[component]["target"]).read_text())
        literals = manifest["configMapGenerator"][0].get("literals", [])
        desired = {
            key: value
            for literal in literals
            for key, separator, value in [literal.partition("=")]
            if separator and key in properties
        }
    return {
        name: _coerce_desired_value(value, properties[name])
        for name, value in desired.items()
    }


def _validate_config_compatibility(
    root: Path, contract: dict[str, Any]
) -> None:
    component = contract["component"]
    if component == "release-automation" or not (
        "image" in contract or "runtime_config" in contract
    ):
        return

    schema_path = _approved_config_schema(root, contract)
    if not schema_path.is_file():
        raise ValueError(
            f"Config schema {contract['config_schema_version']} is not approved for {component}"
        )
    schema_bytes = schema_path.read_bytes()
    actual_digest = "sha256:" + hashlib.sha256(schema_bytes).hexdigest()
    if actual_digest != contract["config_schema_digest"]:
        raise ValueError("Producer config schema digest does not match the GitOps-approved schema")

    schema = json.loads(schema_bytes)
    from jsonschema import Draft202012Validator, ValidationError

    Draft202012Validator.check_schema(schema)
    authoritative_required = set(schema.get("required", []))
    declared_required = set(contract["required_config"])
    if declared_required != authoritative_required:
        raise ValueError(
            "required_config must exactly match the GitOps-approved schema required list"
        )

    runtime_config = contract.get("runtime_config")
    effective_config = (
        runtime_config
        if runtime_config is not None
        else _current_runtime_config(root, component, schema)
    )
    if runtime_config is not None:
        sensitive_keys = sorted(key for key in runtime_config if SENSITIVE_NAME.search(key))
        if sensitive_keys:
            raise ValueError(
                "Secret-like runtime_config keys are forbidden; use secret_refs: "
                + ", ".join(sensitive_keys)
            )
    try:
        Draft202012Validator(schema).validate(effective_config)
    except ValidationError as error:
        location = ".".join(str(part) for part in error.absolute_path) or "runtime_config"
        prefix = "Current desired config" if runtime_config is None else location
        raise ValueError(f"{prefix}: {error.message}") from error

    missing = sorted(declared_required - set(effective_config))
    if missing:
        raise ValueError("Required runtime config is missing: " + ", ".join(missing))
    if (
        component == "inference"
        and effective_config["DRIFT_MIN_SAMPLES"]
        > effective_config["DRIFT_WINDOW_SIZE"]
    ):
        raise ValueError("DRIFT_MIN_SAMPLES cannot exceed DRIFT_WINDOW_SIZE")


def validate_workload_release(root: Path, contract: dict[str, Any]) -> None:
    if (root / "state/recovery-lock.json").exists():
        raise ValueError("Workload release blocked by active operator recovery; see docs/RECOVERY.md")
    validate_contract(contract, "workload-release-v1.schema.json")
    component = contract["component"]
    expected_source = COMPONENTS[component]["source_repository"]
    if contract["source_repository"] != expected_source:
        raise ValueError(f"Component {component} may only be released by {expected_source}")

    if component == "inference":
        manifest = yaml.safe_load((root / COMPONENTS[component]["target"]).read_text())
        predictor = manifest["spec"]["predictor"]
        annotations = manifest.get("metadata", {}).get("annotations", {})
        if (
            predictor.get("canaryTrafficPercent") is not None
            or annotations.get("mlops.iris/candidate-model-version") is not None
        ):
            raise ValueError(
                "Inference workload release is blocked while a model rollout is active"
            )

    if "image" in contract:
        state_path = root / "state/production-platform.json"
        if not state_path.is_file():
            raise ValueError(
                "Platform image allow-list is not initialized; merge platform-reconcile first"
            )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        ecr_key = PLATFORM_ECR_KEYS[component]
        expected_repository = state["ecr_repositories"][ecr_key]["url"]
        if contract["image"]["repository"] != expected_repository:
            raise ValueError(
                f"{component} may only use the Terraform-approved ECR repository "
                f"{expected_repository}"
            )
    elif component == "inference":
        manifest = yaml.safe_load((root / COMPONENTS[component]["target"]).read_text())
        current = manifest["spec"]["predictor"]["containers"][0]["image"]
        if not re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", current):
            raise ValueError("The first inference release must include an immutable image")
    elif component == "model-registry":
        desired = yaml.safe_load((root / COMPONENTS[component]["target"]).read_text())
        image = desired["images"][0]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(image.get("digest", ""))):
            raise ValueError("The first model-registry release must include an immutable image")

    if component == "release-automation":
        unsupported = {"runtime_config", "secret_refs", "config_schema_version"} & set(
            contract
        )
        if unsupported:
            raise ValueError("release-automation accepts an immutable image only")
        if "image" not in contract:
            raise ValueError("release-automation requires an immutable image")

    _validate_config_compatibility(root, contract)

    runtime_config = contract.get("runtime_config")
    duplicate_keys = sorted(set(runtime_config or {}) & set(contract.get("secret_refs", {})))
    if duplicate_keys:
        raise ValueError(
            "A key cannot be both plain runtime_config and secret_refs: "
            + ", ".join(duplicate_keys)
        )


def _render_inference(root: Path, contract: dict[str, Any]) -> list[Path]:
    target = root / COMPONENTS["inference"]["target"]
    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    container = document["spec"]["predictor"]["containers"][0]
    if "image" in contract:
        container["image"] = (
            f"{contract['image']['repository']}@{contract['image']['digest']}"
        )
    if "runtime_config" in contract:
        env = container.setdefault("env", [])
        by_name = {item["name"]: item for item in env}
        for name, value in sorted(contract["runtime_config"].items()):
            rendered = _string_value(value)
            if name in by_name:
                by_name[name]["value"] = rendered
            else:
                env.append({"name": name, "value": rendered})
    if "secret_refs" in contract:
        env_from = [
            item
            for item in container.get("envFrom", [])
            if item.get("secretRef", {}).get("name") != "inference-runtime"
        ]
        if contract["secret_refs"]:
            env_from.append({"secretRef": {"name": "inference-runtime"}})
        if env_from:
            container["envFrom"] = env_from
        else:
            container.pop("envFrom", None)
    target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return [target]


def _render_model_registry(root: Path, contract: dict[str, Any]) -> list[Path]:
    target = root / COMPONENTS["model-registry"]["target"]
    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    if "image" in contract:
        image = document["images"][0]
        image["newName"] = contract["image"]["repository"]
        image.pop("newTag", None)
        image["digest"] = contract["image"]["digest"]
    if "runtime_config" in contract:
        literals = document["configMapGenerator"][0]["literals"]
        positions = {item.split("=", 1)[0]: index for index, item in enumerate(literals)}
        for name, value in sorted(contract["runtime_config"].items()):
            literal = f"{name}={_string_value(value)}"
            if name in positions:
                literals[positions[name]] = literal
            else:
                literals.append(literal)
    target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    targets = [target]
    if "secret_refs" in contract:
        deployment_path = root / COMPONENTS["model-registry"]["deployment"]
        deployment = yaml.safe_load(deployment_path.read_text(encoding="utf-8"))
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        env_from = container.setdefault("envFrom", [])
        secret_entry = {"secretRef": {"name": "model-registry-runtime"}}
        had_runtime_secret = any(
            item.get("secretRef", {}).get("name") == "model-registry-runtime"
            for item in env_from
        )
        env_from[:] = [
            item
            for item in env_from
            if item.get("secretRef", {}).get("name") != "model-registry-runtime"
        ]
        if contract["secret_refs"]:
            env_from.append(secret_entry)
        if contract["secret_refs"] or had_runtime_secret:
            deployment_path.write_text(
                yaml.safe_dump(deployment, sort_keys=False), encoding="utf-8"
            )
            targets.append(deployment_path)
    return targets


def _render_release_automation(root: Path, contract: dict[str, Any]) -> list[Path]:
    target = root / COMPONENTS["release-automation"]["target"]
    current = target.read_text(encoding="utf-8")
    match = re.search(
        r"^\s*- \{name: release-automation-image, value:\s+([^}]+)\}$",
        current,
        flags=re.MULTILINE,
    )
    if match is None:
        raise RuntimeError("Expected one release-automation-image parameter")
    reference = f"{contract['image']['repository']}@{contract['image']['digest']}"
    updated, matches = re.subn(
        r"^(\s*- \{name: release-automation-image, value:)\s+[^}]+(\})$",
        rf"\g<1> {reference}\g<2>",
        current,
        count=1,
        flags=re.MULTILINE,
    )
    if matches != 1:
        raise RuntimeError("Expected one release-automation-image parameter")
    target.write_text(updated, encoding="utf-8")
    return [target]


def _render_external_secret(root: Path, contract: dict[str, Any]) -> list[Path]:
    refs = contract.get("secret_refs")
    if refs is None:
        return []
    component = contract["component"]
    settings = COMPONENTS[component]
    environment_directory = (root / settings["target"]).parent
    target = environment_directory / "runtime-external-secret.yaml"
    secret_name = settings["secret_name"]
    kustomization_path = root / settings["kustomization"]
    kustomization = yaml.safe_load(kustomization_path.read_text(encoding="utf-8"))
    resources = kustomization.setdefault("resources", [])
    if not refs:
        if not target.exists() and target.name not in resources:
            return []
        if target.exists():
            target.unlink()
        if target.name in resources:
            resources.remove(target.name)
        kustomization_path.write_text(
            yaml.safe_dump(kustomization, sort_keys=False), encoding="utf-8"
        )
        return [kustomization_path]

    document = {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {"name": secret_name, "namespace": "mlops"},
        "spec": {
            "refreshInterval": "1h",
            "secretStoreRef": {"kind": "ClusterSecretStore", "name": "aws-secrets-manager"},
            "target": {"name": secret_name, "creationPolicy": "Owner"},
            "data": [
                {
                    "secretKey": name,
                    "remoteRef": {
                        "key": reference["key"],
                        "property": reference["property"],
                    },
                }
                for name, reference in sorted(refs.items())
            ],
        },
    }
    target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    if target.name not in resources:
        resources.append(target.name)
    kustomization_path.write_text(
        yaml.safe_dump(kustomization, sort_keys=False), encoding="utf-8"
    )
    return [target, kustomization_path]


def render_workload_release(root: Path, contract: dict[str, Any]) -> list[Path]:
    validate_workload_release(root, contract)
    component = contract["component"]
    if component == "inference":
        targets = _render_inference(root, contract)
    elif component == "model-registry":
        targets = _render_model_registry(root, contract)
    else:
        targets = _render_release_automation(root, contract)
    targets.extend(_render_external_secret(root, contract))
    return targets


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a production workload release")
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    contract = load_contract(args.contract)
    validate_workload_release(args.root, contract)
    metadata: dict[str, Any] = {
        "component": contract["component"],
        "change_id": contract["change_id"],
        "source_sha": contract["source_sha"],
        "source_repository": contract["source_repository"],
        "facets": [
            name
            for name in ("image", "runtime_config", "secret_refs")
            if name in contract
        ],
    }
    if "image" in contract:
        metadata["image_ref"] = (
            f"{contract['image']['repository']}@{contract['image']['digest']}"
        )
    if not args.validate_only:
        metadata["paths"] = [
            str(path) for path in render_workload_release(args.root, contract)
        ]
    print(json.dumps(metadata, separators=(",", ":")))


if __name__ == "__main__":
    main()
