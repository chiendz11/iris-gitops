import pytest

from scripts.update_dispatcher_image import update_dispatcher_image

MANIFEST = """\
spec:
  arguments:
    parameters:
      - {name: dispatcher-image, value: DISPATCHER_IMAGE_NOT_PUBLISHED}
"""
IMAGE = (
    "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/"
    "iris-mlops-prod/dispatcher@sha256:"
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)


def test_pins_dispatcher_by_digest() -> None:
    rendered = update_dispatcher_image(MANIFEST, IMAGE)
    assert f"value: {IMAGE}" in rendered
    assert "DISPATCHER_IMAGE_NOT_PUBLISHED" not in rendered


@pytest.mark.parametrize(
    "image",
    [
        "dispatcher:latest",
        "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/dispatcher:sha",
        "ghcr.io/example/dispatcher@sha256:" + "0" * 64,
    ],
)
def test_rejects_non_ecr_or_mutable_reference(image: str) -> None:
    with pytest.raises(ValueError):
        update_dispatcher_image(MANIFEST, image)
