import pytest
import yaml

from automation.model_release.renderer import (
    CANDIDATE_VERSION_ANNOTATION,
    STABLE_VERSION_ANNOTATION,
    render_inference_service,
)


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
          - {name: MODEL_VERSION, value: champion}
"""


def predictor(rendered: str) -> dict:
    return yaml.safe_load(rendered)["spec"]["predictor"]


def model_uri(rendered_predictor: dict) -> str:
    return next(
        item["value"]
        for item in rendered_predictor["containers"][0]["env"]
        if item["name"] == "MODEL_URI"
    )


def model_version(rendered_predictor: dict) -> str:
    return str(
        next(
            item["value"]
            for item in rendered_predictor["containers"][0]["env"]
            if item["name"] == "MODEL_VERSION"
        )
    )


def bootstrap(version: str = "6") -> str:
    return render_inference_service(
        MANIFEST,
        model_name="iris-classifier",
        model_version=version,
        baseline_model_version=None,
        action="bootstrap",
    )


def canary(candidate: str = "7", baseline: str = "6") -> str:
    return render_inference_service(
        bootstrap(baseline),
        model_name="iris-classifier",
        model_version=candidate,
        baseline_model_version=baseline,
        action="canary",
    )


def annotations(rendered: str) -> dict[str, str]:
    return yaml.safe_load(rendered)["metadata"]["annotations"]


def test_bootstrap_pins_an_immutable_stable_version() -> None:
    rendered = bootstrap()
    rendered_predictor = predictor(rendered)
    assert "canaryTrafficPercent" not in rendered_predictor
    assert model_uri(rendered_predictor) == "models:/iris-classifier/6"
    assert model_version(rendered_predictor) == "6"
    assert annotations(rendered)[STABLE_VERSION_ANNOTATION] == "6"
    assert CANDIDATE_VERSION_ANNOTATION not in annotations(rendered)


def test_canary_pins_immutable_version_at_ten_percent() -> None:
    rendered = canary()
    rendered_predictor = predictor(rendered)
    assert rendered_predictor["canaryTrafficPercent"] == 10
    assert model_uri(rendered_predictor) == "models:/iris-classifier/7"
    assert model_version(rendered_predictor) == "7"
    assert annotations(rendered)[STABLE_VERSION_ANNOTATION] == "6"
    assert annotations(rendered)[CANDIDATE_VERSION_ANNOTATION] == "7"


def test_promote_removes_canary_field() -> None:
    rendered = render_inference_service(
        canary(),
        model_name="iris-classifier",
        model_version="7",
        baseline_model_version="6",
        action="promote",
    )
    rendered_predictor = predictor(rendered)
    assert "canaryTrafficPercent" not in rendered_predictor
    assert model_uri(rendered_predictor) == "models:/iris-classifier/7"
    assert annotations(rendered)[STABLE_VERSION_ANNOTATION] == "7"
    assert CANDIDATE_VERSION_ANNOTATION not in annotations(rendered)


def test_rollback_restores_baseline_instead_of_retaining_candidate_at_zero() -> None:
    rendered = render_inference_service(
        canary(),
        model_name="iris-classifier",
        model_version="7",
        baseline_model_version="6",
        action="rollback",
    )
    rendered_predictor = predictor(rendered)
    assert "canaryTrafficPercent" not in rendered_predictor
    assert model_uri(rendered_predictor) == "models:/iris-classifier/6"
    assert model_version(rendered_predictor) == "6"
    assert annotations(rendered)[STABLE_VERSION_ANNOTATION] == "6"
    assert CANDIDATE_VERSION_ANNOTATION not in annotations(rendered)


def test_canary_rejects_a_stale_or_invented_baseline() -> None:
    with pytest.raises(RuntimeError, match="baseline does not match"):
        render_inference_service(
            bootstrap("6"),
            model_name="iris-classifier",
            model_version="7",
            baseline_model_version="5",
            action="canary",
        )


@pytest.mark.parametrize("action", ["promote", "rollback"])
def test_promote_and_rollback_require_the_matching_active_canary(action: str) -> None:
    with pytest.raises(RuntimeError, match="matching candidate canary"):
        render_inference_service(
            bootstrap("5"),
            model_name="iris-classifier",
            model_version="7",
            baseline_model_version="6",
            action=action,
        )


@pytest.mark.parametrize("action", ["promote", "rollback"])
def test_terminal_release_rendering_is_idempotent(action: str) -> None:
    first = render_inference_service(
        canary(),
        model_name="iris-classifier",
        model_version="7",
        baseline_model_version="6",
        action=action,
    )
    second = render_inference_service(
        first,
        model_name="iris-classifier",
        model_version="7",
        baseline_model_version="6",
        action=action,
    )
    assert second == first


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
            baseline_model_version="6",
            action=action,
        )
