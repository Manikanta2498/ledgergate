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


def test_journal_dump_prints_every_row_in_order(
    tmp_path: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from ledgergate.journal import Journal
    from ledgergate.ledger import (
        EPOCH,
        USD,
        Account,
        AccountType,
        ChartOfAccounts,
        SequentialIds,
        SteppingClock,
    )

    path = f"{tmp_path}/j.journal"
    chart = ChartOfAccounts([Account("cash", AccountType.ASSET, USD)])
    j = Journal.create(path, chart, clock=SteppingClock(EPOCH), ids=SequentialIds())
    j.handle({"tool": "balance", "call_id": "c", "arguments": {"account": "cash"}})
    j.close()

    assert main(["journal", "dump", path]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    tables = [line["table"] for line in lines]
    assert tables[0] == "journal" and "definition" in tables and "reads" in tables
    seqs = [line["journal_sequence"] for line in lines]
    assert seqs == sorted(seqs)

    assert main(["journal", "dump", path, "--table", "reads"]) == 0
    only = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {line["table"] for line in only} == {"reads"}


def test_journal_dump_on_a_missing_file_fails_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["journal", "dump", "/nonexistent/journal.sqlite"]) == 2
    assert "cannot read journal" in capsys.readouterr().err


def test_journal_without_subcommand_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["journal"])
