"""Regressions pinned from the whole-repository external review of 2026-09-06 (the items
fixed at the journal, codec, CLI and runner; the verifier and release items have their own
suites under tests/unit/invariants, tests/unit/trace, tests/unit/assurance and tests/meta)."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from ledgergate.cli.__main__ import main
from ledgergate.codec.ijson import IJsonError, IJsonRangeError, loads, require_ijson
from ledgergate.derive import trace as derive_trace
from ledgergate.invariants import check as verify
from ledgergate.journal import (
    Journal,
    ThresholdPolicySet,
    generate_signing_key,
    verification_key_text,
)
from ledgergate.journal.auth import AuthError, _parse_expires_at, sign_request
from ledgergate.journal.policy import Threshold
from ledgergate.ledger import (
    EPOCH,
    USD,
    Account,
    AccountType,
    ChartOfAccounts,
    IllegalTransitionError,
    SequentialIds,
    SteppingClock,
    TransactionEvent,
)
from ledgergate.ledger.state import Advance

CHART = ChartOfAccounts(
    [Account("cash", AccountType.ASSET, USD), Account("revenue", AccountType.REVENUE, USD)]
)


def _journal(tmp_path: Path, **kw: Any) -> Journal:
    return Journal.create(
        str(tmp_path / "j.journal"), CHART, clock=SteppingClock(EPOCH), ids=SequentialIds(), **kw
    )


def _post(key: str, call_id: str = "c1") -> dict[str, Any]:
    return {
        "tool": "post",
        "call_id": call_id,
        "key": key,
        "arguments": {
            "draft": {
                "postings": [
                    {"account": "cash", "side": "debit", "money": {"amount": 1, "currency": "USD"}},
                    {
                        "account": "revenue",
                        "side": "credit",
                        "money": {"amount": 1, "currency": "USD"},
                    },
                ]
            }
        },
    }


class TestP1AdvanceRefund:
    """P1-1: `advance` with event `refund` was decodable, committed a rejected row, and left
    the journal non-derivable. It is refused at the codec and at the core, so no row exists."""

    def test_the_core_refuses_the_construction(self) -> None:
        with pytest.raises(IllegalTransitionError):
            Advance("k", "t", TransactionEvent.REFUND)

    def test_admit_commit_derive_verify(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        try:
            r = j.handle(
                {
                    "tool": "advance",
                    "call_id": "c1",
                    "key": "k1",
                    "arguments": {"transaction_id": "t", "event": "refund"},
                }
            )
            assert (r.disposition, r.error_type, r.error_message) == (
                "invalid",
                "malformed_command",
                "arguments.event",
            )
            assert sqlite3.connect(j.path).execute(
                "SELECT COUNT(*) FROM operations"
            ).fetchone() == (0,)
            assert j.handle(_post("k2")).ok
            assert verify(derive_trace(j.path)).status == "pass"
        finally:
            j.close()


class TestAppendOnlyAgainstReplace:
    """`INSERT OR REPLACE` deletes the conflicting row implicitly and SQLite fires no DELETE
    trigger for it on a connection without recursive triggers: a BEFORE INSERT trigger per
    table refuses any insert that would replace a row, on every connection."""

    def test_raw_replace_is_refused_on_every_fact_table(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        j.add_principal("agent", "signed", verification_key_text(generate_signing_key()))
        assert j.handle(_post("k1")).ok
        j.close()
        conn = sqlite3.connect(tmp_path / "j.journal")  # a stranger's connection: no pragmas
        try:
            for table in ("principal_events", "operations", "invocations", "events", "journal"):
                row = conn.execute(
                    f"SELECT * FROM {table} ORDER BY journal_sequence DESC LIMIT 1"
                ).fetchone()
                marks = ",".join("?" * len(row))
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    conn.execute(f"INSERT OR REPLACE INTO {table} VALUES ({marks})", row)
            # a replace that conflicts on a UNIQUE column rather than the primary key
            (seq,) = conn.execute("SELECT MAX(journal_sequence) + 1 FROM journal").fetchone()
            conn.execute(
                "INSERT INTO journal (journal_sequence, kind) VALUES (?, 'operations')", (seq,)
            )
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(
                    "INSERT OR REPLACE INTO operations VALUES (?,?,?,?)",
                    (seq, "k1", "0" * 64, "{}"),
                )
            key = conn.execute(
                "SELECT verification_key FROM principal_events WHERE name = 'agent'"
            ).fetchone()[0]
            assert key is not None  # nothing was replaced
        finally:
            conn.close()


class TestBoundedErrorMessages:
    """An accepted identifier can render (`!r` escapes) past the trace's text bound; that is
    caller input, recorded truncated, never classified as corruption and never a session end."""

    def test_a_wide_identifier_is_a_recorded_rejection(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        try:
            tid = "t" + "\u200b" * 171
            opener = {
                "tool": "open_transaction",
                "call_id": "c1",
                "key": "k1",
                "arguments": {"transaction_id": tid, "amount": {"amount": 5, "currency": "USD"}},
            }
            assert j.handle(opener).ok
            r = j.handle({**opener, "call_id": "c2", "key": "k2"})
            assert (r.disposition, r.response, r.error_type) == (
                "new",
                "rejected",
                "DuplicateTransactionError",
            )
            assert r.error_message is not None and len(r.error_message) == 1024
            assert r.error_message.endswith("[truncated]")
            assert verify(derive_trace(j.path)).status == "pass"
        finally:
            j.close()


class TestCli:
    def test_verify_refuses_to_overwrite_its_source(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        j = _journal(tmp_path)
        j.handle(_post("k1"))
        j.close()
        path = tmp_path / "j.journal"
        before = path.read_bytes()
        assert main(["verify", str(path), "--emit-trace", str(path)]) == 2
        assert "refusing to overwrite" in capsys.readouterr().err
        link = tmp_path / "link.journal"
        link.symlink_to(path)
        assert main(["verify", str(path), "--emit-trace", str(link)]) == 2
        hard = tmp_path / "hard.journal"
        os.link(path, hard)
        assert main(["verify", str(path), "--emit-trace", str(hard)]) == 2
        assert path.read_bytes() == before
        out = tmp_path / "trace.json"
        assert main(["verify", str(path), "--emit-trace", str(out)]) == 0
        assert out.exists()

    def test_sign_reads_bounded_ijson(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seed = tmp_path / "s.seed"
        assert main(["keygen", "--seed-file", str(seed)]) == 0
        capsys.readouterr()
        req = tmp_path / "req.json"
        req.write_text('{"tool":"post","call_id":"c","arguments":{"amount":1,"amount":2}}')
        assert (
            main(
                [
                    "sign",
                    str(req),
                    "--seed-file",
                    str(seed),
                    "--journal-id",
                    "0" * 32,
                    "--principal",
                    "a",
                ]
            )
            == 2
        )
        assert "cannot read input" in capsys.readouterr().err
        req.write_text("[" * 5000 + "]" * 5000)
        assert (
            main(
                [
                    "sign",
                    str(req),
                    "--seed-file",
                    str(seed),
                    "--journal-id",
                    "0" * 32,
                    "--principal",
                    "a",
                ]
            )
            == 2
        )

    def test_pending_reports_unknown_for_a_custom_set(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class CfoOnly(ThresholdPolicySet):
            def approvers_for(self, *_a: Any) -> frozenset[str] | None:
                return frozenset({"cfo"})

        policy = CfoOnly(version="v1", approve_above=[Threshold("open_transaction", "USD", 5_000)])
        cfo, controller = generate_signing_key(), generate_signing_key()
        j = _journal(
            tmp_path,
            policy=policy,
            approvers={
                "cfo": verification_key_text(cfo),
                "controller": verification_key_text(controller),
            },
        )
        try:
            j.handle(
                {
                    "tool": "open_transaction",
                    "call_id": "c1",
                    "key": "k1",
                    "arguments": {
                        "transaction_id": "t",
                        "amount": {"amount": 6000, "currency": "USD"},
                    },
                }
            )
            j.revoke_approver("cfo")
            assert main(["journal", "pending", j.path]) == 0
            row = json.loads(capsys.readouterr().out.strip())
            assert row["admitted_approvers"] == "unknown (set is code)"
            assert "stranded" not in row  # nothing is claimed about a set whose rules are code
        finally:
            j.close()

    def test_a_padded_key_registers_canonically_and_approve_matches(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        private = generate_signing_key()
        canonical = verification_key_text(private)
        padded = canonical + "=" * (-len(canonical) % 4) + "\n"
        j = _journal(tmp_path, approvers={"cfo": padded})
        stored = (
            sqlite3.connect(j.path)
            .execute("SELECT verification_key FROM approver_events WHERE name = 'cfo'")
            .fetchone()[0]
        )
        assert stored == canonical
        j.add_approver("controller", padded.replace("\n", ""))
        rows = (
            sqlite3.connect(j.path)
            .execute("SELECT verification_key FROM approver_events WHERE name = 'controller'")
            .fetchall()
        )
        assert rows == [(canonical,)]
        j.close()
        keyfile = tmp_path / "k.pub"
        keyfile.write_text("not a key\n")
        assert (
            main(
                [
                    "journal",
                    "approver",
                    "add",
                    str(tmp_path / "j.journal"),
                    "x",
                    "--verification-key-file",
                    str(keyfile),
                ]
            )
            == 2
        )
        assert "not an Ed25519 verification key" in capsys.readouterr().err

    def test_approve_refuses_absurd_durations_and_never_echoes_a_seed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        private = generate_signing_key()
        j = _journal(tmp_path, approvers={"cfo": verification_key_text(private)})
        j.close()
        bad_seed = tmp_path / "bad.seed"
        bad_seed.write_bytes(b"\x07" * 5)
        args = [
            str(tmp_path / "j.journal"),
            "--key",
            "k1",
            "--approver",
            "cfo",
            "--approval-id",
            "a1",
        ]
        assert main(["approve", *args, "--seed-file", str(bad_seed)]) == 2
        err = capsys.readouterr().err
        assert "\\x07" not in err and "x07" not in err
        from ledgergate.journal.approvals import private_bytes

        seed = tmp_path / "s.seed"
        seed.write_bytes(private_bytes(private))
        for hours in ("0", "-1", "nan", "1e9"):
            assert main(["approve", *args, "--seed-file", str(seed), "--valid-hours", hours]) == 2
        assert "--valid-hours" in capsys.readouterr().err


class TestGrammarHardening:
    def test_offset_components_are_bounded(self) -> None:
        for bad in ("2026-01-01T00:00:00+00:60", "2026-01-01T00:00:00+24:00"):
            with pytest.raises(AuthError, match="authentication_malformed"):
                _parse_expires_at(bad)
        assert _parse_expires_at("2026-01-01T00:00:00+23:59") is not None

    def test_ijson_diagnostics_are_content_safe_and_bounded_before_allocation(self) -> None:
        with pytest.raises(IJsonError) as exc:
            loads('{"' + "s" * 300 + '": 1, "' + "s" * 300 + '": 2}')
        assert "sss" not in str(exc.value) and "300 characters" in str(exc.value)
        with pytest.raises(IJsonRangeError) as exc2:
            require_ijson({"x": 10**5000})
        assert "bits" in str(exc2.value) and "0000" not in str(exc2.value)
        with pytest.raises(IJsonError, match="exceeds 10 nodes"):
            require_ijson(list(range(1_000_000)), max_nodes=10)

    def test_runner_validates_sign_as_seed(self, tmp_path: Path) -> None:
        import yaml

        from ledgergate.runner import CorpusError, load_corpus

        root = Path("corpus")
        import shutil

        copy = tmp_path / "corpus"
        shutil.copytree(root, copy)
        p = copy / "scenarios" / "red-team" / "signed-with-unregistered-key.yaml"
        doc = yaml.safe_load(p.read_text())
        doc["agent"]["script"][0]["sign_as_seed"] = "not-a-seed"
        p.write_text(yaml.safe_dump(doc))
        with pytest.raises(CorpusError, match="sign_as_seed is not an Ed25519 seed"):
            load_corpus(copy)


class TestSignedRequestStillWorksAfterHardening:
    def test_round_trip(self, tmp_path: Path) -> None:
        private = generate_signing_key()
        j = _journal(tmp_path)
        try:
            j.add_principal("agent", "signed", verification_key_text(private))
            v = _post("k1")
            v["auth"] = sign_request(
                v,
                private=private,
                journal_id=j.definition.journal_id,
                principal="agent",
                expires_at=j.clock.peek() + timedelta(seconds=300),  # type: ignore[attr-defined]
            )
            r = j.handle(v)
            assert r.ok, (r.error_type, r.error_message)
        finally:
            j.close()


class TestSecondPass:
    def test_a_schema_7_journal_is_refused_not_opened_without_its_triggers(
        self, tmp_path: Path
    ) -> None:
        """The no-replace triggers are schema 8; a journal at schema 7 lacks them and is refused
        by the version comparison like every earlier schema, never opened under a guarantee it
        does not have."""
        from ledgergate.journal import SCHEMA_VERSION, ConfigurationError

        assert SCHEMA_VERSION == 8
        j = _journal(tmp_path)
        j.close()
        conn = sqlite3.connect(tmp_path / "j.journal")
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE '%_no_replace'"
        ).fetchall():
            conn.execute(f"DROP TRIGGER {name}")
        # the definition row is append-only, so write the schema-7 state as SQLite allows a
        # schema-7 build would have: through the schema table, not through the row
        conn.execute("DROP TRIGGER definition_no_update")
        conn.execute("UPDATE definition SET schema_version = 7")
        conn.commit()
        conn.close()
        with pytest.raises(
            ConfigurationError, match="journal is schema 7; this process is schema 8"
        ):
            Journal.open(
                str(tmp_path / "j.journal"), clock=SteppingClock(EPOCH), ids=SequentialIds()
            )

    def test_a_combined_policy_message_is_bounded_not_corruption(self, tmp_path: Path) -> None:
        from ledgergate.journal.policy import Decision

        class Verbose(ThresholdPolicySet):
            def evaluate(self, context: Any) -> Decision:
                return Decision("deny", "v1." + "r" * 1000, "w" * 1000)

        j = _journal(tmp_path, policy=Verbose(version="v1"))
        try:
            r = j.handle(_post("k1"))
            assert (r.response, r.error_type) == ("denied", "PolicyDenied")
            assert r.error_message is not None and len(r.error_message) == 1024
        finally:
            j.close()

    def test_sign_refuses_an_oversized_request_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seed = tmp_path / "s.seed"
        assert main(["keygen", "--seed-file", str(seed)]) == 0
        capsys.readouterr()
        req = tmp_path / "req.json"
        with req.open("wb") as f:
            f.write(b'{"tool":"post","call_id":"c","arguments":{"x":"')
            f.write(b"a" * (16 * 1024 * 1024))
            f.write(b'"}}')
        assert (
            main(
                [
                    "sign",
                    str(req),
                    "--seed-file",
                    str(seed),
                    "--journal-id",
                    "0" * 32,
                    "--principal",
                    "a",
                ]
            )
            == 2
        )
        assert "byte bound" in capsys.readouterr().err
