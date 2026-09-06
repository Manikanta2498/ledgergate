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
        # a new unkilled mutant fails; a baselined mutant now killed is stale and fails
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
        assert "new unkilled mutant m.f:b" in out and "stale entry m.f:a: now killed" in out
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
        assert (
            "warning: timeout entry m.h:d killed this run" in out and "bucket changed m.g:c" in out
        )

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
