from __future__ import annotations

import argparse
import re

import mlflow
from mlflow import MlflowClient
from mlflow.exceptions import MlflowException


def promote(
    model_name: str,
    version: str,
    tracking_uri: str,
    expected_current_version: str | None,
    alias: str = "champion",
) -> None:
    if model_name != "iris-classifier" or alias != "champion":
        raise ValueError("Only the owned Iris champion may be reconciled")
    if not re.fullmatch(r"[0-9]+", version) or (
        expected_current_version is not None
        and not re.fullmatch(r"[0-9]+", expected_current_version)
    ):
        raise ValueError("Expected numeric model versions, or none for an absent champion")
    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient()
    candidate = client.get_model_version(model_name, version)
    if candidate.tags.get("quality_gate") != "passed":
        raise RuntimeError(f"Model {model_name} v{version} failed its offline quality gate")

    try:
        current_version = str(client.get_model_version_by_alias(model_name, alias).version)
    except MlflowException as error:
        missing_alias = error.error_code == "RESOURCE_DOES_NOT_EXIST" or (
            error.error_code == "INVALID_PARAMETER_VALUE"
            and str(error).endswith(f"Registered model alias {alias} not found.")
        )
        if not missing_alias:
            raise
        current_version = None

    # A retry after the alias mutation is safe and must not turn into a false
    # failure. Every other mismatch means the baseline captured at train time
    # is stale, so changing the alias would overwrite a concurrent/manual move.
    if current_version == version:
        client.set_model_version_tag(model_name, version, "deployment_status", "promoted")
        return
    if current_version != expected_current_version:
        raise RuntimeError(
            f"Champion precondition failed: expected {expected_current_version!r}, "
            f"found {current_version!r}"
        )
    client.set_registered_model_alias(model_name, alias, version)
    client.set_model_version_tag(model_name, version, "deployment_status", "promoted")


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote a canary that passed online SLOs")
    parser.add_argument("--tracking-uri", required=True)
    parser.add_argument("--model-name", default="iris-classifier")
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--expected-current-version",
        required=True,
        help="Captured champion version, or 'none' during first bootstrap",
    )
    parser.add_argument("--alias", default="champion")
    args = parser.parse_args()
    expected = (
        None
        if args.expected_current_version.lower() == "none"
        else args.expected_current_version
    )
    promote(
        args.model_name,
        args.version,
        args.tracking_uri,
        expected,
        args.alias,
    )


if __name__ == "__main__":
    main()
