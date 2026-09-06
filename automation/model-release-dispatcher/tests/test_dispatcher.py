import pytest

from dispatcher import release_payload


def test_release_payload_is_a_small_stable_contract() -> None:
    assert release_payload(action="canary", model_version="7", change_id="workflow.uid") == {
        "action": "canary",
        "model_version": "7",
        "change_id": "workflow.uid",
    }


@pytest.mark.parametrize(
    ("action", "version", "change_id"),
    [
        ("direct-patch", "7", "workflow.uid"),
        ("canary", "latest", "workflow.uid"),
        ("canary", "7", "../../unsafe"),
    ],
)
def test_release_payload_rejects_invalid_intent(
    action: str, version: str, change_id: str
) -> None:
    with pytest.raises(ValueError):
        release_payload(action=action, model_version=version, change_id=change_id)
