from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import requests

from automation.contracts import load_contract, validate_contract


API_VERSION = "2022-11-28"
MODEL_RELEASE_WORKFLOW = "model-release.yml"


def release_payload(
    *, action: str, change_id: str, model_result: dict[str, Any]
) -> dict[str, str]:
    """Translate the ML-owned result into the small GitOps release-intent contract."""
    validate_contract(model_result, "model-result-v1.schema.json")
    if not model_result["quality_gate_passed"]:
        raise ValueError("A model that failed the offline gate cannot enter release automation")
    model_version = model_result["model_version"]
    if model_version is None:
        raise ValueError("A registered numeric model version is required for release")
    if action not in {"bootstrap", "canary", "promote", "rollback"}:
        raise ValueError(f"Unsupported release action: {action}")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", change_id):
        raise ValueError("change_id contains unsupported characters")

    bootstrap_required = model_result["bootstrap_required"]
    baseline_model_version = model_result["baseline_model_version"]
    if action == "bootstrap":
        if not bootstrap_required or baseline_model_version is not None:
            raise ValueError(
                "bootstrap requires an eligible model result without a champion baseline"
            )
    else:
        if bootstrap_required or baseline_model_version is None:
            raise ValueError(
                f"{action} requires the numeric champion version captured at training time"
            )
        if baseline_model_version == model_version:
            raise ValueError("candidate and baseline model versions must be different")

    payload = {
        "contract_version": "v1",
        "model_name": model_result["model_name"],
        "model_version": model_version,
        "baseline_model_version": baseline_model_version,
        "action": action,
        "change_id": change_id,
        "run_id": model_result["run_id"],
        "dataset_version": model_result["dataset_version"],
        "source_repository": model_result["source_repository"],
        "source_sha": model_result["source_sha"],
    }
    validate_contract(payload, "model-release-v1.schema.json")
    return payload


def app_headers(client_id: str, private_key: str) -> dict[str, str]:
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iat": int((now - timedelta(seconds=60)).timestamp()),
            "exp": int((now + timedelta(minutes=9)).timestamp()),
            "iss": client_id,
        },
        private_key,
        algorithm="RS256",
    )
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VERSION,
    }


def dispatch(
    *,
    repository: str,
    client_id: str,
    private_key: str,
    payload: dict[str, str],
    api_url: str = "https://api.github.com",
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must use owner/name format")

    installation = requests.get(
        f"{api_url}/repos/{repository}/installation",
        headers=app_headers(client_id, private_key),
        timeout=30,
    )
    installation.raise_for_status()
    installation_id = int(installation.json()["id"])

    token_response = requests.post(
        f"{api_url}/app/installations/{installation_id}/access_tokens",
        headers=app_headers(client_id, private_key),
        json={"permissions": {"actions": "write"}},
        timeout=30,
    )
    token_response.raise_for_status()

    response = requests.post(
        f"{api_url}/repos/{repository}/actions/workflows/"
        f"{MODEL_RELEASE_WORKFLOW}/dispatches",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token_response.json()['token']}",
            "X-GitHub-Api-Version": API_VERSION,
        },
        json={
            "ref": "main",
            "inputs": {"contract": json.dumps(payload, separators=(",", ":"))},
        },
        timeout=30,
    )
    response.raise_for_status()
    print(
        f"Dispatched action={payload['action']} model={payload['model_name']} "
        f"version={payload['model_version']} change_id={payload['change_id']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Dispatch a model release intent to GitOps")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--model-result-contract", required=True, type=Path)
    parser.add_argument(
        "--action", required=True, choices=("bootstrap", "canary", "promote", "rollback")
    )
    parser.add_argument("--change-id", required=True)
    args = parser.parse_args()
    model_result = load_contract(args.model_result_contract)
    dispatch(
        repository=args.repository,
        client_id=os.environ["GITHUB_APP_CLIENT_ID"],
        private_key=os.environ["GITHUB_APP_PRIVATE_KEY"],
        payload=release_payload(
            action=args.action,
            change_id=args.change_id,
            model_result=model_result,
        ),
    )


if __name__ == "__main__":
    main()
