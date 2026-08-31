"""Smoke tests for the CLI entry point.

These exist so M0 has a real gate rather than an empty test run: they prove the package
imports, the console script resolves, and the parser is wired up.
"""

from __future__ import annotations

import pytest

from ledgergate import __version__
from ledgergate.cli.__main__ import build_parser, main


def test_version_is_exposed() -> None:
    assert __version__


def test_no_command_prints_help_and_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "ledgergate" in capsys.readouterr().out


def test_unimplemented_command_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["verify"]) == 2
    assert "not implemented yet" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["run", "verify", "record", "report"])
def test_every_documented_subcommand_parses(command: str) -> None:
    args = build_parser().parse_args([command])
    assert args.command == command
