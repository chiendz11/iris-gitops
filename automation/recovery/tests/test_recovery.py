import json
import subprocess
from pathlib import Path

import pytest
import yaml

from automation.model_release.renderer import render_model_release
from automation.recovery.check_pr import check
from automation.recovery.renderer import INFERENCE, LOCK, git, prepare, render
from automation.workload_release.renderer import render_workload_release
from automation.workload_release.tests.test_renderer import inference_contract, isolated_root


def commit(root: Path, message: str) -> str:
    git(root, "add", ".")
    git(root, "commit", "-m", message)
    sha = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/remotes/origin/main", sha)
    return sha


def set_model(root: Path, version: str, *, canary: bool = False) -> None:
    path = root / INFERENCE
    doc = yaml.safe_load(path.read_text())
    for item in doc["spec"]["predictor"]["containers"][0]["env"]:
        if item["name"] == "MODEL_URI":
            item["value"] = f"models:/iris-classifier/{version}"
        if item["name"] == "MODEL_VERSION":
            item["value"] = version
    doc["metadata"]["annotations"]["mlops.iris/stable-model-version"] = "7" if canary else version
    if canary:
        doc["spec"]["predictor"]["canaryTrafficPercent"] = 10
        doc["metadata"]["annotations"]["mlops.iris/candidate-model-version"] = version
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


@pytest.fixture
def history(tmp_path):
    root = isolated_root(tmp_path)
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Recovery Tests")
    git(root, "config", "user.email", "tests@example.invalid")
    render_workload_release(root, inference_contract(root))
    set_model(root, "7")
    old = commit(root, "known good v7 image a")
    new_intent = inference_contract(root)
    new_intent["image"]["digest"] = "sha256:" + "c" * 64
    new_intent["runtime_config"]["DRIFT_WINDOW_SIZE"] = 700
    render_workload_release(root, new_intent)
    set_model(root, "8")
    current = commit(root, "stable v8 image c")
    return root, old, current


def proposal(history, component="model"):
    root, old, current = history
    return prepare(root, component, old, current, "v1", "Incident latency regression", "Verified artifact and compatibility in incident 123")


def test_recovery_after_promotion_only_changes_model(history):
    root, _, base = history
    before = yaml.safe_load((root / INFERENCE).read_text())["spec"]["predictor"]["containers"][0]
    metadata = proposal(history)
    render(root, metadata)
    after = yaml.safe_load((root / INFERENCE).read_text())
    assert after["spec"]["predictor"]["containers"][0]["image"] == before["image"]
    assert "canaryTrafficPercent" not in after["spec"]["predictor"]
    env = {i["name"]: i.get("value") for i in after["spec"]["predictor"]["containers"][0]["env"]}
    assert env["MODEL_URI"] == "models:/iris-classifier/7"
    assert env["DRIFT_WINDOW_SIZE"] == "700"
    assert (root / LOCK).exists()
    check(root, base)
    with pytest.raises(RuntimeError, match="recovery"):
        render_model_release(root, {})


def test_workload_recovery_changes_image_and_config_not_model(history):
    root, _, _ = history
    render(root, proposal(history, "inference"))
    doc = yaml.safe_load((root / INFERENCE).read_text())
    container = doc["spec"]["predictor"]["containers"][0]
    assert container["image"].endswith("a" * 64)
    env = {i["name"]: i.get("value") for i in container["env"]}
    assert env["MODEL_VERSION"] == "8"
    assert env["DRIFT_WINDOW_SIZE"] == "500"
    assert "MLFLOW_TRACKING_URI" in env
    with pytest.raises(ValueError, match="recovery"):
        render_workload_release(root, inference_contract(root))


def test_stale_main_and_unmerged_target_are_refused(history):
    root, old, current = history
    with pytest.raises(ValueError, match="main changed"):
        prepare(root, "model", old, old, "v1", "An incident reason", "Artifact checked before recovery")
    git(root, "switch", "-c", "unmerged")
    (root / "unmerged.txt").write_text("not reviewed")
    unmerged = commit(root, "unmerged target")
    git(root, "switch", "main")
    git(root, "update-ref", "refs/remotes/origin/main", current)
    with pytest.raises(subprocess.CalledProcessError):
        prepare(root, "model", unmerged, current, "v1", "An incident reason", "Artifact checked before recovery")


def test_active_canary_blocks_workload_but_allows_model_recovery(history):
    root, old, _ = history
    set_model(root, "8", canary=True)
    current = commit(root, "active canary")
    updated = root, old, current
    with pytest.raises(ValueError, match="stable"):
        proposal(updated, "inference")
    render(root, proposal(updated))
    doc = yaml.safe_load((root / INFERENCE).read_text())
    assert "canaryTrafficPercent" not in doc["spec"]["predictor"]


def test_stale_recovery_pr_is_rejected(history):
    root, old, _ = history
    render(root, proposal(history))
    with pytest.raises(ValueError, match="Stale recovery"):
        check(root, old)


def test_lock_cannot_be_overwritten(history):
    root, _, _ = history
    render(root, proposal(history))
    current = commit(root, "recovery applied")
    metadata = json.loads((root / LOCK).read_text())
    metadata["target_model_version"] = "99"
    (root / LOCK).write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="replace an active"):
        check(root, current)


def test_workflow_only_proposes_and_requires_operator_preflight():
    path = Path(__file__).resolve().parents[3] / ".github/workflows/recovery.yml"
    text = path.read_text()
    assert "github.ref == 'refs/heads/main'" in text
    assert 'test "${ACTOR}" = "${OWNER}"' in text
    assert 'test "${PREFLIGHT}" = true' in text
    assert "cosign verify" in text
    assert "gh pr create" in text
    for forbidden in ("gh pr merge", "kubectl apply", "argocd app sync", "helm upgrade"):
        assert forbidden not in text


def test_registry_recovery_preserves_platform_endpoints(history):
    root, _, _ = history
    path = root / "environments/production/model-registry/kustomization.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["images"][0] = {
        "name": "iris-mlflow",
        "newName": "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/iris-mlops-prod/mlflow",
        "digest": "sha256:" + "d" * 64,
    }
    doc["configMapGenerator"][0]["literals"].append("MLFLOW_WORKERS=2")
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    old = commit(root, "working registry release")
    doc["images"][0]["digest"] = "sha256:" + "e" * 64
    literals = doc["configMapGenerator"][0]["literals"]
    literals[:] = [
        "POSTGRES_HOST=new-restored-db.internal" if item.startswith("POSTGRES_HOST=")
        else "MLFLOW_WORKERS=4" if item.startswith("MLFLOW_WORKERS=") else item
        for item in literals
    ]
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    current = commit(root, "new registry release")
    render(root, proposal((root, old, current), "model-registry"))
    after = yaml.safe_load(path.read_text())
    assert after["images"][0]["digest"] == "sha256:" + "d" * 64
    assert "POSTGRES_HOST=new-restored-db.internal" in after["configMapGenerator"][0]["literals"]
    assert "MLFLOW_WORKERS=2" in after["configMapGenerator"][0]["literals"]
    assert after["generatorOptions"]["disableNameSuffixHash"] is False


def test_inference_recovery_removes_newer_schema_keys(history):
    root, old, _ = history
    schema = json.loads((root / "contracts/workload-config/inference-v1.schema.json").read_text())
    schema["properties"]["NEW_THRESHOLD"] = {"type": "number"}
    (root / "contracts/workload-config/inference-v2.schema.json").write_text(json.dumps(schema))
    path = root / INFERENCE
    doc = yaml.safe_load(path.read_text())
    doc["spec"]["predictor"]["containers"][0]["env"].append({"name": "NEW_THRESHOLD", "value": "0.5"})
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    current = commit(root, "add a v2 config key")
    render(root, proposal((root, old, current), "inference"))
    assert "NEW_THRESHOLD" not in path.read_text()
