import pytest
import yaml

from scripts.model_rollout import render_inference_service

MANIFEST = """\
apiVersion: serving.kserve.io/v1beta1
kind: InferenceService
metadata:
  name: iris-classifier
spec:
  predictor:
    containers:
      - name: kserve-container
        env:
          - {name: MODEL_URI, value: "models:/iris-classifier@champion"}
"""


def _predictor(rendered: str) -> dict:
    return yaml.safe_load(rendered)["spec"]["predictor"]


def _model_uri(predictor: dict) -> str:
    return next(
        item["value"]
        for item in predictor["containers"][0]["env"]
        if item["name"] == "MODEL_URI"
    )


def test_canary_pins_immutable_version_at_ten_percent() -> None:
    predictor = _predictor(
        render_inference_service(
            MANIFEST,
            model_name="iris-classifier",
            model_version="7",
            action="canary",
        )
    )
    assert predictor["canaryTrafficPercent"] == 10
    assert _model_uri(predictor) == "models:/iris-classifier/7"


def test_promote_removes_canary_field() -> None:
    canary = render_inference_service(
        MANIFEST,
        model_name="iris-classifier",
        model_version="7",
        action="canary",
    )
    predictor = _predictor(
        render_inference_service(
            canary,
            model_name="iris-classifier",
            model_version="7",
            action="promote",
        )
    )
    assert "canaryTrafficPercent" not in predictor
    assert _model_uri(predictor) == "models:/iris-classifier/7"


def test_rollback_pins_candidate_at_zero_percent() -> None:
    predictor = _predictor(
        render_inference_service(
            MANIFEST,
            model_name="iris-classifier",
            model_version="8",
            action="rollback",
        )
    )
    assert predictor["canaryTrafficPercent"] == 0


@pytest.mark.parametrize(
    ("action", "version"),
    [("direct-patch", "7"), ("canary", "latest")],
)
def test_invalid_intent_is_rejected(action: str, version: str) -> None:
    with pytest.raises(ValueError):
        render_inference_service(
            MANIFEST,
            model_name="iris-classifier",
            model_version=version,
            action=action,
        )
