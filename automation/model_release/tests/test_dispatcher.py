import pytest

from automation.model_release.dispatcher import release_payload


MODEL_RESULT = {
    "contract_version": "v1",
    "model_name": "iris-classifier",
    "model_version": "7",
    "baseline_model_version": "6",
    "quality_gate_passed": True,
    "bootstrap_required": False,
    "run_id": "mlflow-run-123",
    "dataset_version": "etag-456",
    "source_repository": "chiendz11/iris-data-pipeline",
    "source_sha": "a" * 40,
    "metrics": {"accuracy": 0.96, "f1_macro": 0.95, "baseline_accuracy": 0.94},
}


def test_result_is_translated_without_gitops_layout() -> None:
    payload = release_payload(
        action="canary", change_id="workflow.uid", model_result=MODEL_RESULT
    )
    assert payload == {
        "contract_version": "v1",
        "model_name": "iris-classifier",
        "model_version": "7",
        "baseline_model_version": "6",
        "action": "canary",
        "change_id": "workflow.uid",
        "run_id": "mlflow-run-123",
        "dataset_version": "etag-456",
        "source_repository": "chiendz11/iris-data-pipeline",
        "source_sha": "a" * 40,
    }


def test_failed_offline_gate_cannot_dispatch() -> None:
    with pytest.raises(ValueError, match="failed the offline gate"):
        release_payload(
            action="canary",
            change_id="workflow.uid",
            model_result={**MODEL_RESULT, "quality_gate_passed": False},
        )


def test_bootstrap_requires_result_without_baseline() -> None:
    result = {
        **MODEL_RESULT,
        "baseline_model_version": None,
        "bootstrap_required": True,
        "metrics": {
            **MODEL_RESULT["metrics"],
            "baseline_accuracy": None,
        },
    }
    payload = release_payload(
        action="bootstrap", change_id="workflow.uid", model_result=result
    )
    assert payload["baseline_model_version"] is None


@pytest.mark.parametrize("action", ["canary", "promote", "rollback"])
def test_non_bootstrap_actions_require_captured_baseline(action: str) -> None:
    with pytest.raises(ValueError, match="baseline_model_version"):
        release_payload(
            action=action,
            change_id="workflow.uid",
            model_result={
                **MODEL_RESULT,
                "baseline_model_version": None,
                "metrics": {
                    **MODEL_RESULT["metrics"],
                    "baseline_accuracy": None,
                },
            },
        )


def test_bootstrap_action_cannot_replace_an_existing_baseline() -> None:
    with pytest.raises(ValueError, match="without a champion baseline"):
        release_payload(
            action="bootstrap", change_id="workflow.uid", model_result=MODEL_RESULT
        )


def test_candidate_must_differ_from_baseline() -> None:
    with pytest.raises(ValueError, match="must be different"):
        release_payload(
            action="canary",
            change_id="workflow.uid",
            model_result={**MODEL_RESULT, "model_version": "6"},
        )
