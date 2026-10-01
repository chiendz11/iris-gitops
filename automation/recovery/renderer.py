from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

import yaml

from automation.workload_release.renderer import (
    COMPONENTS, _coerce_desired_value, render_workload_release, validate_workload_release,
)

LOCK = "state/recovery-lock.json"
INFERENCE = COMPONENTS["inference"]["target"]
SHA = re.compile(r"^[0-9a-f]{40}$")
IMAGE = re.compile(r"^(?P<repository>[^@]+)@(?P<digest>sha256:[0-9a-f]{64})$")


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def historical(root: Path, revision: str, path: str) -> str:
    # Only call with validated full SHAs and internal allow-listed paths.
    return subprocess.check_output(
        ["git", "-C", str(root), "show", f"{revision}:{path}"], text=True,
    )


def model_state(document: dict, *, stable: bool = False) -> str:
    predictor = document["spec"]["predictor"]
    env = {item["name"]: item.get("value") for item in predictor["containers"][0]["env"]}
    version = str(env.get("MODEL_VERSION", ""))
    if not version.isdigit() or env.get("MODEL_URI") != f"models:/iris-classifier/{version}":
        raise ValueError("Recovery requires a consistent numeric model version/URI")
    annotations = document.get("metadata", {}).get("annotations", {})
    if stable and (
        predictor.get("canaryTrafficPercent") is not None
        or annotations.get("mlops.iris/candidate-model-version") is not None
        or annotations.get("mlops.iris/stable-model-version") != version
    ):
        raise ValueError("Select a historical stable release, not an in-progress canary")
    return version


def prepare(
    root: Path, component: str, revision: str, expected_main: str,
    schema_version: str, reason: str, evidence: str,
) -> dict:
    if component not in {"model", "inference", "model-registry"}:
        raise ValueError("Unsupported recovery component")
    if not SHA.fullmatch(revision) or not SHA.fullmatch(expected_main):
        raise ValueError("Use full 40-character Git commit SHAs")
    if not re.fullmatch(r"v[1-9][0-9]*", schema_version):
        raise ValueError("Invalid config schema version")
    if not (10 <= len(reason) <= 1000 and 10 <= len(evidence) <= 2000):
        raise ValueError("Provide a reason and preflight evidence (10+ characters)")
    if (root / LOCK).exists():
        raise ValueError("An active recovery lock must be resolved first")
    if git(root, "rev-parse", "origin/main") != expected_main:
        raise ValueError("main changed; inspect it and dispatch with a fresh expected_main_sha")
    if git(root, "rev-parse", "HEAD") != expected_main:
        raise ValueError("Render from the expected main commit only")
    subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", revision, expected_main],
        check=True,
    )
    metadata = {
        "component": component, "target_git_sha": revision,
        "expected_main_sha": expected_main, "reason": reason, "evidence": evidence,
        "environment": "production", "schema_version": schema_version,
    }
    target_path = INFERENCE if component == "model" else COMPONENTS[component]["target"]
    old = yaml.safe_load(historical(root, revision, target_path))
    current = yaml.safe_load((root / target_path).read_text())
    if component == "model":
        metadata["target_model_version"] = model_state(old, stable=True)
        metadata["expected_model_version"] = model_state(current)
        return metadata

    schema_path = f"contracts/workload-config/{component}-{schema_version}.schema.json"
    schema_bytes = (root / schema_path).read_bytes()
    if historical(root, revision, schema_path).encode() != schema_bytes:
        raise ValueError("Historical schema differs from the immutable approved schema")
    schema = json.loads(schema_bytes)
    if component == "inference":
        model_state(current, stable=True)
        container = old["spec"]["predictor"]["containers"][0]
        raw = {item["name"]: item["value"] for item in container["env"] if "value" in item}
        reference = container["image"]
    else:
        raw = dict(item.split("=", 1) for item in old["configMapGenerator"][0]["literals"])
        image = old["images"][0]
        reference = f"{image['newName']}@{image.get('digest', '')}"
    match = IMAGE.fullmatch(reference)
    if match is None:
        raise ValueError("Historical image must be an immutable digest, not a tag or placeholder")
    config = {
        key: _coerce_desired_value(raw[key], definition)
        for key, definition in schema["properties"].items() if key in raw
    }
    # Never restore secret values; divergent references need a custom reviewed PR.
    secret_path = str(Path(target_path).parent / "runtime-external-secret.yaml")
    has_old_secret = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e", f"{revision}:{secret_path}"],
        capture_output=True,
    ).returncode == 0
    old_secret = yaml.safe_load(historical(root, revision, secret_path)) if has_old_secret else None
    live_secret = yaml.safe_load((root / secret_path).read_text()) if (root / secret_path).exists() else None
    if old_secret != live_secret:
        raise ValueError("Secret references changed; use a targeted reviewed recovery PR")
    metadata["image_ref"] = reference
    metadata["contract"] = {
        "contract_version": "v1", "kind": "workload_release",
        "component": component, "environment": "production",
        "source_repository": COMPONENTS[component]["source_repository"],
        # Internal projection, not a producer intent or a claim about producer commit provenance.
        "source_sha": revision, "change_id": f"recovery-{revision[:12]}",
        "image": match.groupdict(), "runtime_config": config,
        "config_schema_version": schema_version,
        "config_schema_digest": "sha256:" + hashlib.sha256(schema_bytes).hexdigest(),
        "required_config": schema.get("required", []),
    }
    validate_workload_release(root, metadata["contract"])
    return metadata


def render(root: Path, metadata: dict) -> list[Path]:
    checked = prepare(
        root, metadata["component"], metadata["target_git_sha"],
        metadata["expected_main_sha"], metadata["schema_version"],
        metadata["reason"], metadata["evidence"],
    )
    if checked != metadata:
        raise ValueError("Recovery proposal metadata changed after validation")
    component = metadata["component"]
    if component == "model":
        target = root / INFERENCE
        doc = yaml.safe_load(target.read_text())
        version = metadata["target_model_version"]
        predictor = doc["spec"]["predictor"]
        for item in predictor["containers"][0]["env"]:
            if item["name"] == "MODEL_URI":
                item["value"] = f"models:/iris-classifier/{version}"
            elif item["name"] == "MODEL_VERSION":
                item["value"] = version
        predictor.pop("canaryTrafficPercent", None)
        annotations = doc["metadata"].setdefault("annotations", {})
        annotations.pop("mlops.iris/candidate-model-version", None)
        annotations["mlops.iris/stable-model-version"] = version
        target.write_text(yaml.safe_dump(doc, sort_keys=False))
        paths = [target]
    else:
        paths = render_workload_release(root, metadata["contract"])
        schemas = (root / "contracts/workload-config").glob(f"{component}-v*.schema.json")
        owned = set().union(*(set(json.loads(path.read_text())["properties"]) for path in schemas))
        removed = owned - metadata["contract"]["runtime_config"].keys()
        target = root / COMPONENTS[component]["target"]
        doc = yaml.safe_load(target.read_text())
        if component == "inference":
            container = doc["spec"]["predictor"]["containers"][0]
            container["env"] = [item for item in container["env"] if item["name"] not in removed]
        else:
            generator = doc["configMapGenerator"][0]
            generator["literals"] = [item for item in generator["literals"] if item.split("=", 1)[0] not in removed]
        target.write_text(yaml.safe_dump(doc, sort_keys=False))
    lock = root / LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps(metadata, indent=2) + "\n")
    return [*paths, lock]


def main() -> None:
    parser = argparse.ArgumentParser(description="Propose recovery from a reviewed historical release")
    parser.add_argument("--component", required=True)
    parser.add_argument("--target-git-sha", required=True)
    parser.add_argument("--expected-main-sha", required=True)
    parser.add_argument("--schema-version", default="v1")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args()
    metadata = prepare(
        args.root, args.component, args.target_git_sha, args.expected_main_sha,
        args.schema_version, args.reason, args.evidence,
    )
    if args.render:
        render(args.root, metadata)
    print(json.dumps(metadata))


if __name__ == "__main__":
    main()
