"""Run the real repository policy against isolated Git histories (no cloud calls)."""

from pathlib import Path
import subprocess

import pytest


POLICY = Path(__file__).resolve().parents[2] / ".github/scripts/policy-check.sh"
SENSOR = "environments/production/data-pipeline/sensor.yaml"
CONTRACT = "state/production-platform.json"
PLACEHOLDER = "REPLACE_ECR_TRAINING_REPOSITORY"
REPOSITORY = "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/iris-training"
DIGEST = "a" * 64


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git", "-c", "user.name=Policy Tests",
            "-c", "user.email=policy-tests@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def write(repo: Path, path: str, content: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def snapshot(repo: Path) -> str:
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", "Policy test fixture")
    return git(repo, "rev-parse", "HEAD")


def prepare(repo: Path, *, initialized: bool = False, placeholder: bool = True) -> str:
    git(repo, "init", "--quiet")
    write(repo, ".github/workflows/validate.yml", "name: policy-fixture\n")
    # This file already exists: the Sensor exception must not depend on the
    # broader first-time load-balancer/platform-adoption exception.
    write(repo, "applications/platform-aws-load-balancer-controller.yaml", "# fixture\n")
    write(
        repo, "platform/namespaces/namespaces.yaml",
        "kind: Namespace\nmetadata:\n  name: argo\n  annotations:\n"
        "    argocd.argoproj.io/sync-options: Prune=false\n",
    )
    write(
        repo, "environments/production/data-pipeline/workflow-template.yaml",
        f"- {{name: release-automation-image, value: {REPOSITORY}@sha256:{DIGEST}}}\n",
    )
    image = f"{PLACEHOLDER}:source-sha" if placeholder else f"{REPOSITORY}@sha256:{DIGEST}"
    write(repo, SENSOR, f'dataTemplate: "{image}"\n')
    if initialized:
        write(repo, CONTRACT, "{}\n")
    return snapshot(repo)


def check(repo: Path, base: str) -> subprocess.CompletedProcess:
    head = snapshot(repo)
    return subprocess.run(
        ["bash", str(POLICY), base, head],
        cwd=repo, capture_output=True, text=True, check=False,
    )


def test_existing_sensor_placeholder_can_move_from_tag_to_digest_before_contract(tmp_path):
    base = prepare(tmp_path)
    write(tmp_path, SENSOR, f'dataTemplate: "{PLACEHOLDER}@sha256:{DIGEST}"\n')
    result = check(tmp_path, base)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"allowing {PLACEHOLDER}" in result.stdout


@pytest.mark.parametrize("placeholder", [False, True])
def test_sensor_placeholder_is_forbidden_after_contract(tmp_path, placeholder):
    base = prepare(tmp_path, initialized=True, placeholder=placeholder)
    write(tmp_path, SENSOR, f'dataTemplate: "{PLACEHOLDER}@sha256:{DIGEST}"\n')
    result = check(tmp_path, base)
    assert result.returncode == 1
    assert f"This change introduces {PLACEHOLDER}" in result.stdout


def test_resolved_sensor_cannot_reintroduce_placeholder_even_without_contract(tmp_path):
    base = prepare(tmp_path, placeholder=False)
    write(tmp_path, SENSOR, f'dataTemplate: "{PLACEHOLDER}@sha256:{DIGEST}"\n')
    result = check(tmp_path, base)
    assert result.returncode == 1
    assert f"This change introduces {PLACEHOLDER}" in result.stdout


def test_removing_contract_does_not_reopen_bootstrap_exception(tmp_path):
    base = prepare(tmp_path, initialized=True)
    (tmp_path / CONTRACT).unlink()
    write(tmp_path, SENSOR, f'dataTemplate: "{PLACEHOLDER}@sha256:{DIGEST}"\n')
    result = check(tmp_path, base)
    assert result.returncode == 1
    assert f"This change introduces {PLACEHOLDER}" in result.stdout


@pytest.mark.parametrize(
    ("path", "line", "rejected"),
    [
        (
            "environments/production/data-pipeline/other.yaml",
            f'dataTemplate: "{PLACEHOLDER}@sha256:{DIGEST}"', PLACEHOLDER,
        ),
        (SENSOR, 'dataTemplate: "REPLACE_UNEXPECTED"', "REPLACE_UNEXPECTED"),
        (SENSOR, f'image: "{PLACEHOLDER}@sha256:{DIGEST}"', PLACEHOLDER),
        (
            SENSOR,
            f'dataTemplate: "{PLACEHOLDER}@sha256:REPLACE_UNEXPECTED"', PLACEHOLDER,
        ),
    ],
)
def test_sensor_exception_does_not_allow_other_files_fields_or_placeholders(
    tmp_path, path, line, rejected,
):
    base = prepare(tmp_path)
    write(tmp_path, path, line + "\n")
    result = check(tmp_path, base)
    assert result.returncode == 1
    assert f"This change introduces {rejected}" in result.stdout
