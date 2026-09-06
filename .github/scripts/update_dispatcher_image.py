from __future__ import annotations

import argparse
import re
from pathlib import Path

ECR_DIGEST_REFERENCE = re.compile(
    r"[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/"
    r"[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}"
)


def update_dispatcher_image(manifest: str, image: str) -> str:
    """Update only the DevOps-owned dispatcher image parameter."""
    if not ECR_DIGEST_REFERENCE.fullmatch(image):
        raise ValueError("dispatcher image must be an ECR sha256 digest reference")

    updated, matches = re.subn(
        r"^(\s*- \{name: dispatcher-image, value:)\s+[^}]+(\})$",
        rf"\g<1> {image}\g<2>",
        manifest,
        count=1,
        flags=re.MULTILINE,
    )
    if matches != 1:
        raise RuntimeError("Expected exactly one dispatcher-image workflow parameter")
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Pin the dispatcher image digest in GitOps.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()

    current = args.manifest.read_text(encoding="utf-8")
    args.manifest.write_text(
        update_dispatcher_image(current, args.image),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
