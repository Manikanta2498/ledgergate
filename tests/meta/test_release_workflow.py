"""The release and CI workflows must enforce what docs/spec/assurance.md, *Releases*, claims.

These are shape assertions over the workflow files: a workflow cannot be executed here, so the
gate is that the conditions, the presence check and the build command are literally the ones
the contract states. Every claim below is a sentence in assurance.md or the README.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"

# The one condition that means "this run publishes to the production index". The ref alone is
# not it: workflow_dispatch can be started on a tag ref.
PRODUCTION = "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/')"


def _load(path: Path) -> dict[Any, Any]:
    doc: dict[Any, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return doc


@pytest.fixture(scope="module")
def release() -> dict[Any, Any]:
    return _load(RELEASE)


@pytest.fixture(scope="module")
def ci() -> dict[Any, Any]:
    return _load(CI)


def _script(job: dict[Any, Any], needle: str) -> str:
    return "\n".join(step["run"] for step in job["steps"] if needle in str(step.get("run", "")))


class TestTargetSelection:
    """A dispatch is a rehearsal; only a tag *push* publishes to PyPI."""

    def test_the_refuse_job_checks_the_event_and_the_ref_together(
        self, release: dict[Any, Any]
    ) -> None:
        script = _script(release["jobs"]["refuse"], "GITHUB_REF")
        assert '"${GITHUB_EVENT_NAME}" == "push"' in script, (
            "a tag ref must be refused unless the event is a push"
        )
        assert '"${GITHUB_EVENT_NAME}" == "workflow_dispatch"' in script
        assert '"${GITHUB_REF}" == "refs/heads/main"' in script, (
            "a dispatch must be refused unless it runs from main"
        )

    def test_the_index_is_selected_by_event_and_ref_not_by_the_ref_alone(
        self, release: dict[Any, Any]
    ) -> None:
        publish = release["jobs"]["publish"]
        environment = publish["environment"]
        assert PRODUCTION in environment
        assert "'pypi'" in environment and "'testpypi'" in environment
        (step,) = [s for s in publish["steps"] if "repository-url" in str(s.get("with", ""))]
        # the whole expression, exactly: production URL only under the event+ref conjunction
        assert step["with"]["repository-url"] == (
            "${{ " + PRODUCTION + " && 'https://upload.pypi.org/legacy/'"
            " || 'https://test.pypi.org/legacy/' }}"
        )

    def test_the_testpypi_smoke_job_runs_on_every_run_that_is_not_a_tag_push(
        self, release: dict[Any, Any]
    ) -> None:
        assert release["jobs"]["smoke"]["if"].strip() == "${{ !(" + PRODUCTION + ") }}"

    def test_no_production_only_condition_tests_the_ref_alone(self) -> None:
        text = RELEASE.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "startsWith(github.ref, 'refs/tags/')" not in line:
                continue
            if line.lstrip().startswith("#"):
                continue
            assert PRODUCTION in line, line

    def test_the_release_only_jobs_use_the_same_condition(self, release: dict[Any, Any]) -> None:
        for name in ("release-assets", "publish-release"):
            assert release["jobs"][name]["if"].strip() == PRODUCTION


class TestAlreadyPublishedCheck:
    """The presence check is a guard, so an unanswered check is a refusal, not a pass."""

    def test_the_check_reads_the_status_code_and_only_404_continues(
        self, release: dict[Any, Any]
    ) -> None:
        script = _script(release["jobs"]["release-assets"], "pypi.org/pypi/ledgergate")
        assert "-w '%{http_code}'" in script, "the check must read the HTTP status"
        assert "curl -fsS" not in script, (
            "curl -fsS makes a DNS, TLS or 5xx failure indistinguishable from 'absent'"
        )
        assert "404)" in script and "200)" in script and "*)" in script, script
        # exactly one branch continues; the other two exit non-zero
        assert script.count("exit 1") == 2, script

    def test_the_asset_upload_comes_after_the_presence_check(self, release: dict[Any, Any]) -> None:
        steps = release["jobs"]["release-assets"]["steps"]
        check = next(i for i, s in enumerate(steps) if "http_code" in str(s.get("run", "")))
        upload = next(i for i, s in enumerate(steps) if "--clobber" in str(s.get("run", "")))
        assert check < upload


class TestLockedBuildBackend:
    """The build backend is locked and audited like every other dependency."""

    def test_the_build_system_requirement_is_pinned_and_mirrored_by_a_locked_group(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        (requirement,) = pyproject["build-system"]["requires"]
        assert "==" in requirement, "an unpinned build backend is resolved outside the lock"
        assert requirement in pyproject["dependency-groups"]["build"], (
            "the pin must be mirrored by a dependency group so uv.lock carries it"
        )
        name, version = requirement.split("==")
        lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
        locked = [p for p in lock["package"] if p["name"] == name]
        assert [p["version"] for p in locked] == [version], (
            f"{name} {version} must be in uv.lock, so pip-audit sees the build backend"
        )

    def test_the_release_builds_without_isolation_from_the_locked_group(
        self, release: dict[Any, Any]
    ) -> None:
        script = "\n".join(str(s.get("run", "")) for s in release["jobs"]["build"]["steps"])
        assert "uv sync --locked --only-group build" in script
        assert "uv build --no-build-isolation" in script


class TestInstalledWheelGate:
    """The editable install CI tests is not the artefact users get."""

    def test_ci_runs_the_corpus_from_an_installed_wheel_outside_the_checkout(
        self, ci: dict[Any, Any]
    ) -> None:
        job = ci["jobs"]["wheel"]
        script = "\n".join(str(s.get("run", "")) for s in job["steps"])
        assert "uv build --wheel --no-build-isolation" in script
        assert "uv venv /tmp/wheelenv" in script, "the environment must not live in the checkout"
        assert "uv pip install --python /tmp/wheelenv dist/*.whl" in script, (
            "the wheel is installed with its runtime dependencies only"
        )
        assert "cd /tmp/wheelsmoke" in script, "the smoke must run from another directory"
        assert "/tmp/wheelenv/bin/ledgergate --version" in script
        assert "/tmp/wheelenv/bin/ledgergate run --corpus" in script
        assert 's["pass"] == expected' in script and 's["scenarios"] == expected' in script
        assert 's["fail"] == s["error"] == s["skipped"] == 0' in script

    def test_the_gate_runs_on_every_pull_request(self, ci: dict[Any, Any]) -> None:
        triggers = ci[True]  # PyYAML reads the `on:` key as the boolean true
        assert "pull_request" in triggers and "workflow_call" in triggers
        assert "wheel" in ci["jobs"]

    def test_the_release_publish_depends_on_the_wheel_gate(self, release: dict[Any, Any]) -> None:
        jobs = release["jobs"]
        assert jobs["gates"]["uses"] == "./.github/workflows/ci.yml", (
            "the release runs the CI workflow whole, so it runs the wheel gate too"
        )
        assert "gates" in jobs["build"]["needs"]
        assert "build" in jobs["publish"]["needs"]


def test_the_expected_scenario_count_is_the_corpus_count() -> None:
    """The CI gate derives the count from the corpus rather than hard-coding it, so it cannot
    go stale; this asserts the corpus is the 25 scenarios the README and assurance.md state."""
    assert len(list((ROOT / "corpus" / "scenarios").rglob("*.yaml"))) == 25
