"""docs/spec/assurance.md: the deterministic Hypothesis profile is active under CI and under
mutmut, so a mutation survivor is reproducible."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from hypothesis import settings


@pytest.mark.parametrize(
    ("env", "profile", "suppressed"),
    [({"CI": "true"}, "ci", "False"), ({"MUTANT_UNDER_TEST": ""}, "mutation", "True")],
)
def test_ci_profile_is_loaded_under_ci_and_mutmut(
    env: dict[str, str], profile: str, suppressed: str
) -> None:
    code = (
        "import tests.conftest; from hypothesis import HealthCheck, settings;"
        " s = settings(); print(s.derandomize, s.database is None, settings._current_profile,"
        " HealthCheck.differing_executors in s.suppress_health_check)"
    )
    clean = {k: v for k, v in os.environ.items() if k not in ("CI", "MUTANT_UNDER_TEST")}
    out = subprocess.run(
        [sys.executable, "-c", code],
        env={**clean, **env},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip() == f"True True {profile} {suppressed}"


def test_the_ci_profile_is_hypothesis_own() -> None:
    ci = settings.get_profile("ci")
    assert ci.derandomize and ci.database is None and ci.deadline is None
