from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import yaml

from automation.contracts import load_contract, validate_contract


CANARY_TRAFFIC_PERCENT = 10
STABLE_VERSION_ANNOTATION = "mlops.iris/stable-model-version"
CANDIDATE_VERSION_ANNOTATION = "mlops.iris/candidate-model-version"
ACTIONS = {"bootstrap", "canary", "promote", "rollback"}


def validate_model_release(contract: dict[str, Any]) -> None:
    validate_contract(contract, "model-release-v1.schema.json")


def render_inference_service(
    manifest: str,
    *,
    model_name: str,
    model_version: str,
    baseline_model_version: str | None,
    action: str,
) -> str:
    """Translate a model-release intent into GitOps-owned KServe desired state."""
    if model_name != "iris-classifier":
        raise ValueError("Only the owned iris-classifier model may be rendered")
    if not re.fullmatch(r"[0-9]+", model_version):
        raise ValueError("model_version must be a numeric MLflow version")
    if action not in ACTIONS:
        raise ValueError(f"Unsupported release action: {action}")
    if action == "bootstrap":
        if baseline_model_version is not None:
            raise ValueError("bootstrap must not declare a baseline_model_version")
    elif baseline_model_version is None or not re.fullmatch(
        r"[0-9]+", baseline_model_version
    ):
        raise ValueError(f"{action} requires a numeric baseline_model_version")
    if baseline_model_version == model_version:
        raise ValueError("candidate and baseline model versions must be different")

    document = yaml.safe_load(manifest)
    if document.get("kind") != "InferenceService":
        raise RuntimeError("Expected a KServe InferenceService document")
    predictor = document["spec"]["predictor"]
    env = predictor["containers"][0]["env"]
    by_name = {item["name"]: item for item in env}
    if "MODEL_URI" not in by_name or "MODEL_VERSION" not in by_name:
        raise RuntimeError("InferenceService must declare MODEL_URI and MODEL_VERSION")

    current_version = str(by_name["MODEL_VERSION"]["value"])
    current_traffic = predictor.get("canaryTrafficPercent")
    annotations = document["metadata"].setdefault("annotations", {})
    stable_annotation = annotations.get(STABLE_VERSION_ANNOTATION)
    candidate_annotation = annotations.get(CANDIDATE_VERSION_ANNOTATION)

    current_is_stable = current_traffic is None
    current_is_expected_canary = (
        current_version == model_version
        and current_traffic == CANARY_TRAFFIC_PERCENT
        and stable_annotation == baseline_model_version
        and candidate_annotation == model_version
    )

    if action == "bootstrap":
        already_bootstrapped = (
            current_is_stable
            and current_version == model_version
            and stable_annotation == model_version
            and candidate_annotation is None
        )
        if current_version != "champion" and not already_bootstrapped:
            raise RuntimeError(
                "bootstrap refused because production already has a numeric stable model"
            )
        target_version = model_version
        target_traffic = None
        target_stable_version = model_version
    elif action == "canary":
        stable_matches_contract = (
            current_is_stable
            and current_version == baseline_model_version
            and stable_annotation in {None, baseline_model_version}
            and candidate_annotation is None
        )
        if not stable_matches_contract and not current_is_expected_canary:
            raise RuntimeError(
                "canary baseline does not match the production stable desired state"
            )
        target_version = model_version
        target_traffic = CANARY_TRAFFIC_PERCENT
        target_stable_version = baseline_model_version
    elif action == "promote":
        already_promoted = (
            current_is_stable
            and current_version == model_version
            and stable_annotation == model_version
            and candidate_annotation is None
        )
        if not current_is_expected_canary and not already_promoted:
            raise RuntimeError(
                "promote refused because the matching candidate canary is not active"
            )
        target_version = model_version
        target_traffic = None
        target_stable_version = model_version
    else:
        already_rolled_back = (
            current_is_stable
            and current_version == baseline_model_version
            and stable_annotation == baseline_model_version
            and candidate_annotation is None
        )
        if not current_is_expected_canary and not already_rolled_back:
            raise RuntimeError(
                "rollback refused because the matching candidate canary is not active"
            )
        target_version = baseline_model_version
        target_traffic = None
        target_stable_version = baseline_model_version

    model_uri = f"models:/{model_name}/{target_version}"
    by_name["MODEL_URI"]["value"] = model_uri
    by_name["MODEL_VERSION"]["value"] = target_version
    annotations[STABLE_VERSION_ANNOTATION] = target_stable_version
    if target_traffic is None:
        predictor.pop("canaryTrafficPercent", None)
        annotations.pop(CANDIDATE_VERSION_ANNOTATION, None)
    else:
        predictor["canaryTrafficPercent"] = target_traffic
        annotations[CANDIDATE_VERSION_ANNOTATION] = model_version

    rendered = yaml.safe_dump(document, sort_keys=False)
    verified_document = yaml.safe_load(rendered)
    verified = verified_document["spec"]["predictor"]
    verified_env = {item["name"]: item["value"] for item in verified["containers"][0]["env"]}
    if verified_env["MODEL_URI"] != model_uri:
        raise RuntimeError("Rendered MODEL_URI failed structural validation")
    if str(verified_env["MODEL_VERSION"]) != target_version:
        raise RuntimeError("Rendered MODEL_VERSION failed structural validation")
    if verified.get("canaryTrafficPercent") != target_traffic:
        raise RuntimeError("Rendered canary traffic failed structural validation")
    verified_annotations = verified_document["metadata"]["annotations"]
    if verified_annotations.get(STABLE_VERSION_ANNOTATION) != target_stable_version:
        raise RuntimeError("Rendered stable model annotation failed structural validation")
    expected_candidate = model_version if target_traffic is not None else None
    if verified_annotations.get(CANDIDATE_VERSION_ANNOTATION) != expected_candidate:
        raise RuntimeError("Rendered candidate annotation failed structural validation")
    return rendered


def render_model_release(root: Path, contract: dict[str, Any]) -> Path:
    if (root / "state/recovery-lock.json").exists():
        raise RuntimeError("Model release blocked by active operator recovery; see docs/RECOVERY.md")
    validate_model_release(contract)
    target = root / "environments/production/inference-service/inferenceservice.yaml"
    current = target.read_text(encoding="utf-8")
    target.write_text(
        render_inference_service(
            current,
            model_name=contract["model_name"],
            model_version=contract["model_version"],
            baseline_model_version=contract["baseline_model_version"],
            action=contract["action"],
        ),
        encoding="utf-8",
    )
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a model-release intent into GitOps")
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    contract = load_contract(args.contract)
    validate_model_release(contract)
    metadata = {
        "action": contract["action"],
        "change_id": contract["change_id"],
        "model_name": contract["model_name"],
        "model_version": contract["model_version"],
        "baseline_model_version": contract["baseline_model_version"],
    }
    if not args.validate_only:
        metadata["path"] = str(render_model_release(args.root, contract))
    print(json.dumps(metadata, separators=(",", ":")))


if __name__ == "__main__":
    main()
