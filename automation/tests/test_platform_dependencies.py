from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def load(relative_path: str) -> dict:
    return yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))


def sync_wave(application: dict) -> int:
    return int(application["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])


def test_cert_manager_is_pinned_and_precedes_kserve() -> None:
    cert_manager = load("applications/platform-cert-manager.yaml")
    kserve = load("applications/platform-kserve.yaml")

    assert cert_manager["spec"]["source"] == {
        "repoURL": "https://charts.jetstack.io",
        "chart": "cert-manager",
        "targetRevision": "v1.21.2",
        "helm": {
            "releaseName": "cert-manager",
            "valuesObject": {
                "crds": {"enabled": True, "keep": True},
                "prometheus": {
                    "enabled": True,
                    "servicemonitor": {
                        "enabled": True,
                        "labels": {"release": "monitoring"},
                    },
                },
            },
        },
    }
    assert sync_wave(cert_manager) < sync_wave(kserve)


def test_cert_manager_is_reachable_from_the_app_of_apps() -> None:
    root = load("applications/kustomization.yaml")
    project = load("applications/project.yaml")
    namespaces = list(
        yaml.safe_load_all(
            (ROOT / "platform/namespaces/namespaces.yaml").read_text(encoding="utf-8")
        )
    )

    assert "platform-cert-manager.yaml" in root["resources"]
    assert "https://charts.jetstack.io" in project["spec"]["sourceRepos"]
    cert_manager_namespace = next(
        document
        for document in namespaces
        if document["metadata"]["name"] == "cert-manager"
    )
    assert cert_manager_namespace["metadata"]["annotations"] == {
        "argocd.argoproj.io/sync-options": "Prune=false"
    }


def test_knative_ignores_only_fields_owned_by_its_webhook_controller() -> None:
    knative = load("applications/platform-knative.yaml")
    ignored = knative["spec"]["ignoreDifferences"]

    assert knative["spec"]["syncPolicy"]["syncOptions"] == [
        "ServerSideApply=true",
        "RespectIgnoreDifferences=true",
    ]
    assert {
        (item["kind"], item["name"]): item["managedFieldsManagers"]
        for item in ignored
    } == {
        (
            "MutatingWebhookConfiguration",
            "webhook.serving.knative.dev",
        ): ["webhook"],
        (
            "ValidatingWebhookConfiguration",
            "config.webhook.serving.knative.dev",
        ): ["webhook"],
        (
            "ValidatingWebhookConfiguration",
            "validation.webhook.serving.knative.dev",
        ): ["webhook"],
    }


def test_kserve_v019_predictor_uses_supported_metadata_field() -> None:
    inference_service = load(
        "environments/production/inference-service/inferenceservice.yaml"
    )
    predictor = inference_service["spec"]["predictor"]

    assert "podMetadata" not in predictor
    assert predictor["annotations"] == {
        "prometheus.io/scrape": "true",
        "prometheus.io/path": "/metrics",
        "prometheus.io/port": "8080",
    }
