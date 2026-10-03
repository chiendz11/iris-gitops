"""Keep automated Docker updates within the validated Python runtime line."""

from pathlib import Path

import yaml


def test_python_minor_upgrades_require_coordinated_review():
    root = Path(__file__).resolve().parents[2]
    config = yaml.safe_load((root / ".github/dependabot.yml").read_text())
    docker = next(
        update for update in config["updates"]
        if update["package-ecosystem"] == "docker"
        and update["directory"] == "/automation/model_release"
    )
    rule = next(item for item in docker["ignore"] if item["dependency-name"] == "python")
    assert set(rule["update-types"]) == {
        "version-update:semver-major", "version-update:semver-minor"
    }
    assert docker["open-pull-requests-limit"] > 0
