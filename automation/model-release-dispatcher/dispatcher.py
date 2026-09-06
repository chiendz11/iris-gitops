from __future__ import annotations

import argparse
import os
import re
from datetime import UTC, datetime, timedelta

import jwt
import requests

API_VERSION = "2022-11-28"


def release_payload(*, action: str, model_version: str, change_id: str) -> dict[str, str]:
    """Build the only contract shared by orchestration and GitOps release automation."""
    if action not in {"bootstrap", "canary", "promote", "rollback"}:
        raise ValueError(f"Unsupported release action: {action}")
    if not re.fullmatch(r"[0-9]+", model_version):
        raise ValueError("model_version must be a numeric MLflow model version")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", change_id):
        raise ValueError("change_id contains unsupported characters")
    return {"action": action, "model_version": model_version, "change_id": change_id}


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
        raise ValueError("repository must use the owner/name format")

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
        json={"permissions": {"contents": "write"}},
        timeout=30,
    )
    token_response.raise_for_status()

    response = requests.post(
        f"{api_url}/repos/{repository}/dispatches",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token_response.json()['token']}",
            "X-GitHub-Api-Version": API_VERSION,
        },
        json={"event_type": "model_release", "client_payload": payload},
        timeout=30,
    )
    response.raise_for_status()
    print(
        f"Dispatched model release intent action={payload['action']} "
        f"version={payload['model_version']} change_id={payload['change_id']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Dispatch a model-release intent to GitOps.")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--model-version", required=True)
    parser.add_argument(
        "--action", required=True, choices=("bootstrap", "canary", "promote", "rollback")
    )
    parser.add_argument("--change-id", required=True)
    args = parser.parse_args()
    dispatch(
        repository=args.repository,
        client_id=os.environ["GITHUB_APP_CLIENT_ID"],
        private_key=os.environ["GITHUB_APP_PRIVATE_KEY"],
        payload=release_payload(
            action=args.action,
            model_version=args.model_version,
            change_id=args.change_id,
        ),
    )


if __name__ == "__main__":
    main()
