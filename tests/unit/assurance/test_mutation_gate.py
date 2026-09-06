"""The mutation ratchet script against synthetic runs, and the README's stated count."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "mutation_gate", ROOT / "scripts" / "mutation_gate.py"
)
assert SPEC is not None and SPEC.loader is not None
gate_mod = importlib.util.module_from_spec(SPEC)
sys.modules["mutation_gate"] = gate_mod
SPEC.loader.exec_module(gate_mod)


def _current(*entries: tuple[str, str]) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "function": key.split(":")[0],
            "bucket": bucket,
            "names": [f"{key.split(':')[0]}__mutmut_1"],
        }
        for key, bucket in entries
    }


@pytest.fixture
def in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gate_mod, "BASELINE", tmp_path / ".mutation-baseline.json")
    return tmp_path


class TestRatchet:
    def test_three_fates_and_new_unkilled(
        self, in_tmp: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run1 = _current(
            ("m.f:a", "survived"), ("m.f:b", "killed"), ("m.g:c", "no tests"), ("m.h:d", "timeout")
        )
        assert gate_mod.write_baseline(run1) == 0
        base = json.loads(gate_mod.BASELINE.read_text())
        assert set(base["unkilled"]) == {"m.f:a", "m.g:c", "m.h:d"}
        assert gate_mod.gate(run1) == 0
        # a new unkilled mutant fails; a baselined mutant killed this run is stale and warns
        assert (
            gate_mod.gate(
                _current(
                    ("m.f:a", "killed"),
                    ("m.f:b", "survived"),
                    ("m.g:c", "no tests"),
                    ("m.h:d", "timeout"),
                )
            )
            == 1
        )
        out = capsys.readouterr().out
        assert "new unkilled mutant m.f:b" in out and "warning: stale entry m.f:a" in out
        assert (
            gate_mod.gate(
                _current(
                    ("m.f:a", "killed"),
                    ("m.f:b", "killed"),
                    ("m.g:c", "no tests"),
                    ("m.h:d", "timeout"),
                )
            )
            == 0
        )
        # a vanished key fails in every bucket
        assert gate_mod.gate(_current(("m.f:a", "survived"), ("m.h:d", "timeout"))) == 1
        assert "vanished" in capsys.readouterr().out
        # a killed timeout entry warns only; a bucket change warns only
        assert (
            gate_mod.gate(
                _current(
                    ("m.f:a", "survived"),
                    ("m.f:b", "killed"),
                    ("m.g:c", "survived"),
                    ("m.h:d", "killed"),
                )
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "warning: stale entry m.h:d" in out and "bucket changed m.g:c" in out

    def test_baseline_regeneration_preserves_killed_timeouts_and_drops_vanished_equivalents(
        self, in_tmp: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        gate_mod.BASELINE.write_text(
            json.dumps(
                {
                    "unkilled": {"m.h:d": {"function": "m.h", "bucket": "timeout", "example": "x"}},
                    "equivalent": {
                        "m.e:z": {"reason": "renames a local"},
                        "m.gone:q": {"reason": "old"},
                    },
                }
            )
        )
        run = _current(("m.h:d", "killed"), ("m.e:z", "survived"), ("m.f:a", "survived"))
        assert gate_mod.write_baseline(run) == 0
        base = json.loads(gate_mod.BASELINE.read_text())
        assert "m.h:d" in base["unkilled"] and base["unkilled"]["m.h:d"]["bucket"] == "timeout"
        assert "m.e:z" not in base["unkilled"] and set(base["equivalent"]) == {"m.e:z"}
        out = capsys.readouterr().out
        assert "preserved timeout entry" in out and "dropped equivalent" in out
        assert gate_mod.gate(run) == 0


class TestCheckedInBaseline:
    def test_readme_states_the_baseline_count(self) -> None:
        baseline = json.loads((ROOT / ".mutation-baseline.json").read_text())
        total = len(baseline["unkilled"])
        readme = (ROOT / "README.md").read_text()
        m = re.search(r"(\d+) unkilled mutants", readme)
        assert m is not None, "README must state the mutation baseline count"
        assert int(m.group(1)) == total

    def test_baseline_shape(self) -> None:
        baseline = json.loads((ROOT / ".mutation-baseline.json").read_text())
        assert set(baseline) == {"_", "unkilled", "equivalent"}
        for key, entry in baseline["unkilled"].items():
            assert entry["bucket"] in gate_mod.BUCKETS and key.startswith(entry["function"] + ":")
        for entry in baseline["equivalent"].values():
            assert entry["reason"]


class TestCleanRunViability:
    @pytest.mark.slow
    def test_the_unit_suite_runs_from_a_mutmut_shaped_copy(self, tmp_path: Path) -> None:
        """mutmut runs the tests from mutants/, which holds source_paths, tests/, pyproject.toml,
        uv.lock and also_copy; a unit test reading anything else breaks the clean run and the
        nightly gate with it. Shape the copy exactly as mutmut does and run the suite."""
        import os
        import shutil
        import subprocess
        import tomllib

        cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["mutmut"]
        copy = tmp_path / "mutants"
        copy.mkdir()
        for rel in [*cfg["source_paths"], *cfg["also_copy"], "tests", "pyproject.toml", "uv.lock"]:
            src = ROOT / rel
            if src.is_dir():
                shutil.copytree(
                    src, copy / rel, ignore=shutil.ignore_patterns("__pycache__", ".hypothesis")
                )
            elif src.exists():
                shutil.copy2(src, copy / rel)
        env = {**os.environ, "MUTANT_UNDER_TEST": "stats", "PYTHONPATH": str(copy / "src")}
        env.pop("CI", None)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-x",
                *cfg[
                    "pytest_add_cli_args"
                ],  # the same selection mutmut uses, `-m not slow` included
                *cfg["pytest_add_cli_args_test_selection"],
            ],
            cwd=copy,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]


def test_readme_bucket_breakdown_matches_the_baseline() -> None:
    baseline = json.loads((ROOT / ".mutation-baseline.json").read_text())
    buckets: dict[str, int] = {}
    for entry in baseline["unkilled"].values():
        buckets[entry["bucket"]] = buckets.get(entry["bucket"], 0) + 1
    readme = (ROOT / "README.md").read_text()
    if buckets == {"survived": len(baseline["unkilled"])}:
        assert "all `survived`; none `no tests`" in readme
    else:
        for bucket, n in buckets.items():
            assert f"{n} `{bucket}`" in readme, (bucket, n)


class TestSecondImplementationReview:
    def test_killed_equivalents_are_dropped_and_overlap_fails(
        self, in_tmp: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        gate_mod.BASELINE.write_text(
            json.dumps({"unkilled": {}, "equivalent": {"m.e:z": {"reason": "renames a local"}}})
        )
        assert gate_mod.write_baseline(_current(("m.e:z", "killed"), ("m.f:a", "survived"))) == 0
        base = json.loads(gate_mod.BASELINE.read_text())
        assert base["equivalent"] == {} and "dropped equivalent" in capsys.readouterr().out
        gate_mod.BASELINE.write_text(
            json.dumps(
                {
                    "unkilled": {
                        "m.f:a": {"function": "m.f", "bucket": "survived", "example": "x"}
                    },
                    "equivalent": {"m.f:a": {"reason": "also here"}},
                }
            )
        )
        assert gate_mod.gate(_current(("m.f:a", "survived"))) == 1
        assert "both unkilled and equivalent" in capsys.readouterr().out

    def test_readme_total_is_checked_by_the_gate(
        self, in_tmp: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (in_tmp / "README.md").write_text("baseline: **1 unkilled mutants** of 1,697 (all")
        run = _current(("m.f:a", "survived"), ("m.f:b", "killed"))
        gate_mod.write_baseline(run)
        assert gate_mod.gate(run) == 1
        assert "README says of 1,697 mutants, this run has 2" in capsys.readouterr().out
        (in_tmp / "README.md").write_text("baseline: **1 unkilled mutants** of 2 (all")
        assert gate_mod.gate(run) == 0

    def test_baseline_keys_are_disjoint(self) -> None:
        baseline = json.loads((ROOT / ".mutation-baseline.json").read_text())
        assert not set(baseline["unkilled"]) & set(baseline["equivalent"])


def test_results_listing_is_parsed_and_must_cover_every_mutant() -> None:
    parsed = gate_mod._parse_results("    m.f__mutmut_1: killed\n    m.f__mutmut_2: no tests\n\n")
    assert parsed == {"m.f__mutmut_1": "killed", "m.f__mutmut_2": "no tests"}
