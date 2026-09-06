from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

ACTIONS = {
    "bootstrap": None,
    "canary": 10,
    "promote": None,
    "rollback": 0,
}


def render_inference_service(
    manifest: str,
    *,
    model_name: str,
    model_version: str,
    action: str,
) -> str:
    """Translate the stable release-intent contract into GitOps-owned desired state."""
    if action not in ACTIONS:
        raise ValueError(f"Unsupported release action: {action}")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model_name):
        raise ValueError("model_name contains unsupported characters")
    if not re.fullmatch(r"[0-9]+", model_version):
        raise ValueError("model_version must be a numeric MLflow model version")

    model_uri = f"models:/{model_name}/{model_version}"
    updated, uri_matches = re.subn(
        r'(^\s*- \{name: MODEL_URI, value: ")[^"]+("\}\s*$)',
        rf"\g<1>{model_uri}\g<2>",
        manifest,
        count=1,
        flags=re.MULTILINE,
    )
    if uri_matches != 1:
        raise RuntimeError("Expected exactly one MODEL_URI entry in the InferenceService")

    canary_pattern = r"^\s{4}canaryTrafficPercent:\s*[0-9]+\s*\n"
    updated, canary_matches = re.subn(canary_pattern, "", updated, flags=re.MULTILINE)
    if canary_matches > 1:
        raise RuntimeError("InferenceService contains more than one canaryTrafficPercent field")

    traffic_percent = ACTIONS[action]
    if traffic_percent is not None:
        updated, predictor_matches = re.subn(
            r"^(\s{2}predictor:\s*\n)",
            rf"\g<1>    canaryTrafficPercent: {traffic_percent}\n",
            updated,
            count=1,
            flags=re.MULTILINE,
        )
        if predictor_matches != 1:
            raise RuntimeError("Expected exactly one predictor block in the InferenceService")

    parsed = yaml.safe_load(updated)
    predictor = parsed["spec"]["predictor"]
    env = predictor["containers"][0]["env"]
    rendered_uri = next(item["value"] for item in env if item["name"] == "MODEL_URI")
    if rendered_uri != model_uri:
        raise RuntimeError("Rendered MODEL_URI did not pass structural validation")
    if predictor.get("canaryTrafficPercent") != traffic_percent:
        raise RuntimeError("Rendered canary traffic did not pass structural validation")
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a model-release intent into GitOps.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--model-name", default="iris-classifier")
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--action", required=True, choices=tuple(ACTIONS))
    args = parser.parse_args()

    current = args.manifest.read_text(encoding="utf-8")
    args.manifest.write_text(
        render_inference_service(
            current,
            model_name=args.model_name,
            model_version=args.model_version,
            action=args.action,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
