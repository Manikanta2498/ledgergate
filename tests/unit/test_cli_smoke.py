"""Smoke tests for the CLI entry point.

These exist so M0 has a real gate rather than an empty test run: they prove the package
imports, the console script resolves, and the parser is wired up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ledgergate import __version__
from ledgergate.cli.__main__ import build_parser, main


def test_version_is_exposed() -> None:
    assert __version__


def test_no_command_prints_help_and_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "ledgergate" in capsys.readouterr().out


def test_unimplemented_command_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run"]) == 2
    assert "not implemented yet" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["run", "record", "report"])
def test_every_documented_subcommand_parses(command: str) -> None:
    args = build_parser().parse_args([command])
    assert args.command == command


def test_verify_parses_with_a_source() -> None:
    args = build_parser().parse_args(["verify", "trace.json", "--json"])
    assert args.command == "verify" and args.json and args.source == "trace.json"


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


def test_journal_pending_and_approve_round_trip(
    tmp_path: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from cryptography.hazmat.primitives import serialization

    from ledgergate.journal import (
        Journal,
        Threshold,
        ThresholdPolicySet,
        generate_signing_key,
        verification_key_text,
    )
    from ledgergate.ledger import (
        EPOCH,
        USD,
        Account,
        AccountType,
        ChartOfAccounts,
        SequentialIds,
        SteppingClock,
    )

    signer = generate_signing_key()
    key_file = Path(f"{tmp_path}/approver.key")
    key_file.write_bytes(
        signer.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )
    path = f"{tmp_path}/j.journal"
    chart = ChartOfAccounts(
        [Account("cash", AccountType.ASSET, USD), Account("revenue", AccountType.REVENUE, USD)]
    )
    policy = ThresholdPolicySet(
        version="p1", approve_above=[Threshold("open_transaction", "USD", 100)]
    )
    j = Journal.create(
        path,
        chart,
        clock=SteppingClock(EPOCH),
        ids=SequentialIds(),
        policy=policy,
        approval_key=verification_key_text(signer),
    )
    request = {
        "tool": "open_transaction",
        "call_id": "c1",
        "key": "big",
        "arguments": {"transaction_id": "t", "amount": {"amount": 500, "currency": "USD"}},
    }
    assert j.handle(request).response == "awaiting_approval"

    assert main(["journal", "pending", path]) == 0
    (line,) = capsys.readouterr().out.splitlines()
    assert json.loads(line)["key"] == "big"

    assert (
        main(
            [
                "approve",
                path,
                "--key",
                "big",
                "--approver",
                "cfo",
                "--approval-id",
                "a1",
                "--signing-key",
                str(key_file),
            ]
        )
        == 0
    )
    artefact = json.loads(capsys.readouterr().out)
    assert (
        artefact["amount"] == "500"
        and artefact["currency"] == "USD"
        and artefact["subject"] == "t"  # copied from the stored command
    )

    class WallClock:
        def now(self):  # type: ignore[no-untyped-def]
            from datetime import UTC, datetime

            return datetime.now(UTC)

    j.close()
    j = Journal.open(path, clock=WallClock(), ids=SequentialIds(start=5), policy=policy)
    r = j.handle({**request, "call_id": "c2", "approval": artefact})
    assert (r.disposition, r.response) == ("approval", "applied")
    j.close()

    assert _approve(path, "a1", str(key_file)) == 1  # consumed id
    assert "already been consumed" in capsys.readouterr().err
    assert _approve(path, "a2", str(key_file)) == 1  # nothing pending
    assert "awaiting approval" in capsys.readouterr().err
    other = Path(f"{tmp_path}/other.key")
    other.write_bytes(
        generate_signing_key().private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )
    assert _approve(path, "a3", str(other)) == 1  # wrong signing key
    assert "does not match" in capsys.readouterr().err
    assert main(["journal", "pending", path]) == 0 and capsys.readouterr().out == ""


def _approve(path: str, approval_id: str, key_file: str) -> int:
    return main(
        [
            "approve",
            path,
            "--key",
            "big",
            "--approver",
            "cfo",
            "--approval-id",
            approval_id,
            "--signing-key",
            key_file,
        ]
    )
