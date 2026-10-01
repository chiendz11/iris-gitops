from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml


WORKFLOW_TEMPLATE = Path(
    "environments/production/data-pipeline/workflow-template.yaml"
)


def load_templates() -> dict[str, dict]:
    document = yaml.safe_load(WORKFLOW_TEMPLATE.read_text(encoding="utf-8"))
    return {template["name"]: template for template in document["spec"]["templates"]}


def dag_tasks() -> dict[str, dict]:
    lifecycle = load_templates()["lifecycle"]
    return {task["name"]: task for task in lifecycle["dag"]["tasks"]}


def parameter_value(task: dict, name: str) -> str:
    parameters = task["arguments"]["parameters"]
    return next(item["value"] for item in parameters if item["name"] == name)


def test_registry_alias_moves_only_after_stable_rollout_is_ready() -> None:
    tasks = dag_tasks()

    assert tasks["propose-bootstrap-rollout"]["depends"] == "train.Succeeded"
    assert (
        tasks["bootstrap-promote-registry"]["depends"]
        == "bootstrap-smoke.Succeeded"
    )

    assert tasks["propose-rollout-100"]["depends"] == "observe-canary.Succeeded"
    assert tasks["bootstrap-smoke"]["depends"] == "wait-bootstrap-rollout.Succeeded"
    assert tasks["promote-registry"]["depends"] == "stable-smoke.Succeeded"
    assert tasks["stable-smoke"]["depends"] == "wait-rollout-100.Succeeded"


def test_only_explicit_rejection_can_propose_rollback() -> None:
    tasks = dag_tasks()
    assert tasks["propose-rollback"]["depends"] == "observe-canary.Succeeded"
    assert "== 'reject'" in tasks["propose-rollback"]["when"]
    document = yaml.safe_load(WORKFLOW_TEMPLATE.read_text())
    assert document["spec"]["onExit"] == "report-status"


def test_runtime_ownership_and_contract_handoff() -> None:
    templates = load_templates()
    for name in ("smoke", "observe", "promote-registry-alias", "dispatch-release"):
        container = templates[name]["container"]
        assert container["image"] == "{{workflow.parameters.release-automation-image}}"
        assert container["command"][2].startswith("automation.model_release.")
    assert templates["train"]["container"]["command"][2] == "iris_pipeline.training.train"
    assert "--model-result-contract" in templates["dispatch-release"]["container"]["args"]
    assert "--model-version" in templates["observe"]["container"]["args"]


def test_only_dispatcher_reads_the_secret_created_by_external_secrets() -> None:
    document = yaml.safe_load(WORKFLOW_TEMPLATE.read_text())
    external = yaml.safe_load(
        Path("environments/production/data-pipeline/external-secret.yaml").read_text()
    )
    assert document["metadata"]["namespace"] == external["metadata"]["namespace"]
    secret_name = external["spec"]["target"]["name"]
    secret_keys = {item["secretKey"] for item in external["spec"]["data"]}
    dispatcher_refs = []
    for name, template in load_templates().items():
        for variable in template.get("container", {}).get("env", []):
            ref = variable.get("valueFrom", {}).get("secretKeyRef")
            if ref is None:
                continue
            assert name == "dispatch-release", "Training/evaluation must not receive App keys"
            assert ref["name"] == secret_name
            assert ref["key"] in secret_keys
            dispatcher_refs.append(ref["key"])
    assert sorted(dispatcher_refs) == ["client_id", "private_key"]


def test_recovery_finalizes_only_after_ready_and_smoke() -> None:
    steps = load_templates()["recovery-finalize"]["steps"]
    assert [step[0]["template"] for step in steps] == [
        "wait-rollout", "smoke", "promote-registry-alias",
    ]
    doc = yaml.safe_load(WORKFLOW_TEMPLATE.read_text())
    assert doc["spec"]["synchronization"]["mutexes"] == [{"name": "iris-model-promotion"}]


def test_rollback_waits_for_exact_baseline_as_stable_state() -> None:
    tasks = dag_tasks()
    wait_rollback = tasks["wait-rollback"]

    assert wait_rollback["depends"] == "propose-rollback.Succeeded"
    assert parameter_value(wait_rollback, "traffic") == "absent"
    assert parameter_value(wait_rollback, "version") == (
        "{{tasks.train.outputs.parameters.baseline-model-version}}"
    )


def test_rollout_observer_has_read_only_kubernetes_commands() -> None:
    command = load_templates()["wait-rollout"]["container"]["args"][0]

    assert "kubectl get" in command
    assert ".status.observedGeneration" in command
    assert '@.type=="Ready"' in command
    for mutation in ("kubectl apply", "kubectl patch", "kubectl delete"):
        assert mutation not in command


def test_validation_schema_and_observer_client_match_cluster_profile() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/validate.yml").read_text())
    steps = workflow["jobs"]["render-schema"]["steps"]
    install = next(step for step in steps if step.get("name") == "Install kubectl")
    assert install["with"]["version"] == "v1.34.0"
    assert any("-kubernetes-version 1.34.0" in step.get("run", "") for step in steps)
    observer = load_templates()["wait-rollout"]["container"]["image"]
    version = re.search(r":1\.(\d+)(?:[.@-]|$)", observer)
    assert version is not None
    assert abs(int(version[1]) - 34) <= 1, "Observer kubectl must remain within one minor of EKS"


def test_smoke_url_matches_serving_http_contract_not_registry_model_name() -> None:
    document = yaml.safe_load(WORKFLOW_TEMPLATE.read_text())
    parameters = {item["name"]: item["value"] for item in document["spec"]["arguments"]["parameters"]}
    url = urlsplit(parameters["inference-url"])
    assert url.hostname == "iris-classifier-predictor.mlops.svc.cluster.local"
    assert url.path == "/v1/models/iris:predict"
    args = load_templates()["smoke"]["container"]["args"]
    assert args[args.index("--url") + 1] == "{{workflow.parameters.inference-url}}"


def test_training_workspace_uses_dynamic_encrypted_topology_aware_storage() -> None:
    document = yaml.safe_load(WORKFLOW_TEMPLATE.read_text())
    claims = document["spec"]["volumeClaimTemplates"]
    assert len(claims) == 1
    claim = claims[0]
    storage = yaml.safe_load(Path("platform/storage/training-storage-class.yaml").read_text())
    assert claim["metadata"]["name"] == "workspace"
    assert claim["spec"]["storageClassName"] == storage["metadata"]["name"]
    assert claim["spec"]["resources"]["requests"]["storage"] == "1Gi"
    assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert "volumeName" not in claim["spec"]
    assert storage["provisioner"] == "ebs.csi.aws.com"
    assert storage["parameters"] == {
        "type": "gp3", "encrypted": "true", "csi.storage.k8s.io/fstype": "ext4",
    }
    assert storage["volumeBindingMode"] == "WaitForFirstConsumer"
    assert storage["allowVolumeExpansion"] is True
    assert storage["reclaimPolicy"] == "Delete"
    assert storage["metadata"]["annotations"]["storageclass.kubernetes.io/is-default-class"] == "false"
    assert document["spec"]["volumeClaimGC"]["strategy"] == "OnWorkflowCompletion"


def test_storage_is_wired_into_root_application() -> None:
    root = yaml.safe_load(Path("applications/kustomization.yaml").read_text())
    assert "platform-storage.yaml" in root["resources"]
    app = yaml.safe_load(Path("applications/platform-storage.yaml").read_text())
    assert app["spec"]["source"]["path"] == "platform/storage"
    assert app["spec"]["syncPolicy"]["automated"] == {"prune": True, "selfHeal": True}
    assert int(app["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]) < 0
