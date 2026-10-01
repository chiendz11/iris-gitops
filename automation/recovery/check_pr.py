"""Require a recovery PR to be regenerated if main changed after inspection."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from automation.recovery.renderer import LOCK, SHA


def check(root: Path, base: str) -> None:
    if not SHA.fullmatch(base):
        raise ValueError("Expected a full base SHA")
    path = root / LOCK
    if not path.exists():
        return  # Unlock is an ordinary protected operator PR, after verification.
    previous = subprocess.run(
        ["git", "-C", str(root), "show", f"{base}:{LOCK}"],
        capture_output=True, text=True,
    )
    current = json.loads(path.read_text())
    if previous.returncode == 0:
        if json.loads(previous.stdout) != current:
            raise ValueError("Do not replace an active recovery lock")
        return
    if current["expected_main_sha"] != base:
        raise ValueError("Stale recovery PR: main changed; close and dispatch a fresh proposal")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    check(Path.cwd(), args.base)
