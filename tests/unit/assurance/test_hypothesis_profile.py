"""docs/spec/assurance.md: the deterministic Hypothesis profile is active under CI and under
mutmut, so a mutation survivor is reproducible."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from hypothesis import settings


@pytest.mark.parametrize("env", [{"CI": "true"}, {"MUTANT_UNDER_TEST": ""}])
def test_ci_profile_is_loaded_under_ci_and_mutmut(env: dict[str, str]) -> None:
    code = (
        "import tests.conftest; from hypothesis import settings;"
        " s = settings(); print(s.derandomize, s.database is None)"
    )
    clean = {k: v for k, v in os.environ.items() if k not in ("CI", "MUTANT_UNDER_TEST")}
    out = subprocess.run(
        [sys.executable, "-c", code],
        env={**clean, **env},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip() == "True True"


def test_the_ci_profile_is_hypothesis_own() -> None:
    ci = settings.get_profile("ci")
    assert ci.derandomize and ci.database is None and ci.deadline is None
