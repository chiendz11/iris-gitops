from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_DIRECTORY = REPOSITORY_ROOT / "contracts"


def load_contract(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Contract root must be a JSON object")
    return payload


def validate_contract(payload: dict[str, Any], schema_name: str) -> None:
    schema_path = CONTRACTS_DIRECTORY / schema_name
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        Draft202012Validator(schema).validate(payload)
    except ValidationError as error:
        location = ".".join(str(part) for part in error.absolute_path) or "contract"
        raise ValueError(f"{location}: {error.message}") from error
