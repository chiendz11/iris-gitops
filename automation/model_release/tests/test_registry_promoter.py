import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

# The production image installs MLflow. Renderer unit tests use a tiny import
# seam so CI need not download the full MLflow dependency graph twice.
fake_mlflow = ModuleType("mlflow")
fake_mlflow.set_tracking_uri = Mock()  # type: ignore[attr-defined]
fake_exceptions = ModuleType("mlflow.exceptions")


class FakeMlflowException(Exception):
    def __init__(self, message: str, *, error_code: str = "INTERNAL_ERROR") -> None:
        super().__init__(message)
        self.error_code = error_code


fake_exceptions.MlflowException = FakeMlflowException  # type: ignore[attr-defined]
fake_mlflow.MlflowClient = Mock  # type: ignore[attr-defined]
sys.modules.setdefault("mlflow", fake_mlflow)
sys.modules.setdefault("mlflow.exceptions", fake_exceptions)

registry_promoter = importlib.import_module("automation.model_release.registry_promoter")


def _client(*, current: str | None, quality_gate: str = "passed") -> Mock:
    client = Mock()
    client.get_model_version.return_value = SimpleNamespace(
        tags={"quality_gate": quality_gate}
    )
    if current is None:
        client.get_model_version_by_alias.side_effect = registry_promoter.MlflowException(
            "missing alias", error_code="RESOURCE_DOES_NOT_EXIST"
        )
    else:
        client.get_model_version_by_alias.return_value = SimpleNamespace(version=current)
    return client


def test_promote_requires_the_captured_champion(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(current="6")
    monkeypatch.setattr(registry_promoter, "MlflowClient", lambda: client)

    registry_promoter.promote("iris-classifier", "7", "http://mlflow", "6")

    client.set_registered_model_alias.assert_called_once_with(
        "iris-classifier", "champion", "7"
    )


def test_promote_rejects_stale_champion(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(current="8")
    monkeypatch.setattr(registry_promoter, "MlflowClient", lambda: client)

    with pytest.raises(RuntimeError, match="Champion precondition failed"):
        registry_promoter.promote("iris-classifier", "7", "http://mlflow", "6")

    client.set_registered_model_alias.assert_not_called()


def test_bootstrap_requires_no_existing_champion(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(current=None)
    monkeypatch.setattr(registry_promoter, "MlflowClient", lambda: client)

    registry_promoter.promote("iris-classifier", "1", "http://mlflow", None)

    client.set_registered_model_alias.assert_called_once()


def test_retry_after_success_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(current="7")
    monkeypatch.setattr(registry_promoter, "MlflowClient", lambda: client)

    registry_promoter.promote("iris-classifier", "7", "http://mlflow", "6")

    client.set_registered_model_alias.assert_not_called()
    client.set_model_version_tag.assert_called_once_with(
        "iris-classifier", "7", "deployment_status", "promoted"
    )


def test_mlflow_error_is_not_treated_as_missing_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(current="6")
    client.get_model_version_by_alias.side_effect = registry_promoter.MlflowException(
        "server unavailable"
    )
    monkeypatch.setattr(registry_promoter, "MlflowClient", lambda: client)

    with pytest.raises(registry_promoter.MlflowException, match="server unavailable"):
        registry_promoter.promote("iris-classifier", "7", "http://mlflow", "6")

    client.set_registered_model_alias.assert_not_called()
