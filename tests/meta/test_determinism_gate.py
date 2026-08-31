"""The determinism gate must actually catch violations.

A checker that never fires is worse than no checker, because it reads as protection.
This runs the real AST logic over synthetic bad input.
"""

from __future__ import annotations

from pathlib import Path

from scripts.check_determinism import check_file


def test_flags_banned_import(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import random\n", encoding="utf-8")
    assert any("random" in v for v in check_file(bad))


def test_flags_wall_clock_call(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import datetime\nx = datetime.now()\n", encoding="utf-8")
    violations = check_file(bad)
    assert any("datetime.now()" in v for v in violations)


def test_clean_file_passes(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    assert check_file(good) == []
