"""The journal's write and read protocols, tested family by family against the spec.

Each class below corresponds to one of the test families the specification implies.
Assertions are made against the rows in the database, not only the returned response,
because the rows are the record.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from tests.unit.journal.support import CHART, balance, count, open_txn, post, rows

from ledgergate.journal import (
    FACT_TABLES,
    ConfigurationError,
    EffectError,
    IdentityAdmitter,
    Journal,
    JournalError,
    NullPolicySet,
)
from ledgergate.journal.policy import Decision, PolicyContext
from ledgergate.ledger import (
    EPOCH,
    USD,
    Account,
    AccountType,
    ChartOfAccounts,
    SequentialIds,
    SteppingClock,
)


class TestGlobalSequenceAndAppendOnly:  # family 12
    def test_every_fact_row_has_a_journal_row_of_its_kind(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        journal.handle(post("k1"))
        journal.handle(balance("cash"))
        for table in FACT_TABLES:
            for seq, *_rest in rows(raw, table):
                (kind,) = raw.execute(
                    "SELECT kind FROM journal WHERE journal_sequence = ?", (seq,)
                ).fetchone()
                assert kind == table

    def test_sequence_is_strictly_increasing_across_tables(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        journal.handle(post("k1"))
        journal.handle(post("k2"))
        seqs = [
            s for (s, _k) in raw.execute("SELECT journal_sequence, kind FROM journal ORDER BY 1")
        ]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

    @pytest.mark.parametrize("table", [*FACT_TABLES, "journal"])
    def test_no_update_or_delete_succeeds(
        self, journal: Journal, raw: sqlite3.Connection, table: str
    ) -> None:
        journal.handle(post("k1"))
        journal.handle(balance("cash"))
        if not count(raw, table):  # triggers fire per row; approvals stay empty in M2b
            pytest.skip(f"{table} has no rows to mutate under the null policy")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(f"DELETE FROM {table}")
        if count(raw, table):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                raw.execute(f"UPDATE {table} SET journal_sequence = journal_sequence")

    def test_allocation_cannot_be_consumed_by_the_wrong_table(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        raw.execute("BEGIN")
        seq = raw.execute("INSERT INTO journal (kind) VALUES ('events')").lastrowid
        with pytest.raises(sqlite3.IntegrityError, match="wrong table"):
            raw.execute("INSERT INTO reads VALUES (?, 1, 0, 'h', 'd')", (seq,))
        raw.execute("ROLLBACK")


class TestNewOperation:  # families 3, 7, 10
    def test_row_sequence_for_an_applied_command(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        r = journal.handle(post("k1"))
        assert (r.disposition, r.response, r.ok) == ("new", "applied", True)
        kinds = [
            k
            for (_s, k) in raw.execute(
                "SELECT journal_sequence, kind FROM journal WHERE kind != 'definition' ORDER BY 1"
            )
        ]
        assert kinds == [
            "operations",
            "invocations",
            "events",
            "decisions",
            "outcomes",
            "invocation_responses",
            "events",
        ]
        (op,) = rows(raw, "operations")
        (inv,) = rows(raw, "invocations")
        (out,) = rows(raw, "outcomes")
        (resp,) = rows(raw, "invocation_responses")
        assert inv[1] == op[0] and out[1] == op[0] and out[2] is None  # root outcome
        assert resp[1] == inv[0] and resp[3] == out[0] and resp[4] == "applied"
        assert out[3] == "applied" and out[6] == "e-000001" and out[8] != out[9]

    def test_every_new_operation_gets_a_fully_specified_null_policy_decision(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        journal.handle(post("k1"))
        (dec,) = rows(raw, "decisions")
        context = json.loads(dec[3])
        assert (dec[4], dec[5], dec[6]) == ("none", "allow", "none.allow_all")
        assert dec[7] == "null policy set: no rules configured"
        assert context["principal"] == "local" and context["subject"] is None
        assert context["digest_kind"] == "fingerprint" and len(context["command_digest"]) == 64
        assert context["aggregates"] == {} and context["approval"] is None
        assert (
            dec[8] is None and dec[9] is None and dec[10] is None
        )  # no presentation, verdict, consumption

    def test_core_rejection_is_a_recorded_terminal_outcome(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        r = journal.handle(
            {
                "tool": "refund",
                "call_id": "c",
                "key": "k",
                "arguments": {"transaction_id": "none", "money": {"amount": 1, "currency": "USD"}},
            }
        )
        assert (r.response, r.ok, r.error_type) == ("rejected", False, "UnknownTransactionError")
        (out,) = rows(raw, "outcomes")
        assert out[3] == "rejected" and out[4] == "UnknownTransactionError" and out[8] == out[9]
        # the key is spent: a retry replays the rejection
        again = journal.handle(
            {
                "tool": "refund",
                "call_id": "c2",
                "key": "k",
                "arguments": {"transaction_id": "none", "money": {"amount": 1, "currency": "USD"}},
            }
        )
        assert (again.disposition, again.response, again.error_type) == (
            "replay",
            "replayed",
            "UnknownTransactionError",
        )
        assert count(raw, "outcomes") == 1

    def test_null_policy_returns_allow_for_every_context(self) -> None:
        policy = NullPolicySet()
        for digest_kind in ("fingerprint", "request"):
            ctx = PolicyContext("local", None, "0" * 64, digest_kind, EPOCH, "none")
            assert policy.evaluate(ctx) == Decision(
                "allow", "none.allow_all", "null policy set: no rules configured"
            )
        assert not policy.gates_read("balance") and not policy.gates_read("trial_balance")


class TestReplayAndConflict:  # family 4
    def test_replay_names_the_outcome_that_answered_and_writes_no_decision(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        first = journal.handle(post("k1"))
        second = journal.handle(post("k1", call_id="retry"))
        assert (second.disposition, second.response, second.ok) == ("replay", "replayed", True)
        assert (
            second.result["entry_id"] == first.result["entry_id"]
            and second.result["replayed"] is True
        )
        assert (
            count(raw, "operations") == 1
            and count(raw, "outcomes") == 1
            and count(raw, "decisions") == 1
        )
        inv = rows(raw, "invocations")[1]
        resp = rows(raw, "invocation_responses")[1]
        assert inv[4] == "replay" and inv[1] == rows(raw, "operations")[0][0]
        assert resp[3] == first.outcome and resp[4] == "replayed"
        assert journal.ledger.sequence == 1

    def test_conflict_records_both_sides_and_no_outcome(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        journal.handle(post("k1"))
        r = journal.handle(post("k1", call_id="c2", amount=5))
        assert (r.disposition, r.response, r.ok, r.error_type) == (
            "conflict",
            "conflict",
            False,
            "IdempotencyConflictError",
        )
        inv = rows(raw, "invocations")[1]
        (op,) = rows(raw, "operations")
        assert inv[4] == "conflict" and inv[5] != op[2]  # attempted fingerprint differs
        assert json.loads(inv[6])["draft"]["postings"][0]["money"]["amount"] == 5
        assert rows(raw, "invocation_responses")[1][3] is None
        assert count(raw, "outcomes") == 1

    def test_replay_survives_reopen(self, journal: Journal, reopen: Callable[[], Journal]) -> None:
        first = journal.handle(post("k1"))
        journal.close()
        again = reopen()
        r = again.handle(post("k1", call_id="after-restart"))
        assert r.response == "replayed" and r.result["entry_id"] == first.result["entry_id"]
        again.close()


class TestInvalidAdmission:  # family 2
    @pytest.mark.parametrize(
        ("value", "code", "path"),
        [
            ("not an object", "not_an_object", "$"),
            (
                {"tool": "post", "call_id": "c", "key": "k"},
                "malformed_command",
                "arguments.draft",
            ),
            (
                {"tool": "teleport", "call_id": "c", "key": "k", "arguments": {}},
                "unknown_tool",
                "tool",
            ),
            (
                {"tool": "post", "call_id": "a\nb", "key": "k", "arguments": {}},
                "invalid_identifier",
                "call_id",
            ),
            (
                {"tool": "post", "call_id": "c", "key": "k", "arguments": {}, "bogus": 1},
                "unknown_field",
                "$",
            ),
            (
                {"tool": "balance", "call_id": "c", "key": "k", "arguments": {}},
                "unexpected_field",
                "key",
            ),
            (
                {
                    "tool": "post",
                    "call_id": "c",
                    "key": "k",
                    "approval": {"id": 1},
                    "arguments": {},
                },
                "approval_unsupported",
                "approval",
            ),
        ],
    )
    def test_invalid_input_is_recorded_without_an_operation(
        self, journal: Journal, raw: sqlite3.Connection, value: object, code: str, path: str
    ) -> None:
        r = journal.handle(value)
        assert (r.disposition, r.response, r.ok) == ("invalid", "invalid", False)
        assert r.error_message == f"{code} at {path}"
        assert (
            count(raw, "operations") == 0
            and count(raw, "outcomes") == 0
            and count(raw, "decisions") == 0
        )
        (inv,) = rows(raw, "invocations")
        assert inv[4] == "invalid" and inv[1] is None and inv[7] is None  # no request_digest
        inbound, outbound = rows(raw, "events")
        envelope = json.loads(inbound[3])
        assert envelope["error"] == {"code": code, "path": path}
        assert len(envelope["input_digest"]) == 64
        assert len(envelope["payload"]) <= 4096
        assert json.loads(outbound[3])["ok"] is False
        (resp,) = rows(raw, "invocation_responses")
        assert resp[3] is None and resp[4] == "invalid"

    def test_invalid_envelope_keeps_call_id_only_when_it_is_a_valid_identifier(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        journal.handle({"tool": "post", "call_id": "fine", "key": "k", "arguments": {}})
        journal.handle({"tool": "post", "call_id": "bad\rid", "key": "k", "arguments": {}})
        first, second = rows(raw, "invocations")
        assert first[8] == "fine" and second[8] is None

    def test_approvals_tables_stay_empty_in_m2b(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        journal.handle(
            {
                "tool": "post",
                "call_id": "c",
                "key": "k",
                "approval": {"approval_id": "a"},
                "arguments": {},
            }
        )
        assert count(raw, "approvals") == 0 and count(raw, "approval_consumptions") == 0


class TestReads:  # family 5
    def test_audited_read_records_cursor_head_and_digest(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        journal.handle(post("k1"))
        r = journal.handle(balance("cash"))
        assert (r.disposition, r.response, r.ok) == ("read", "read", True)
        assert r.result["balance"] == {"amount": "1999", "currency": "USD"}
        (rd,) = rows(raw, "reads")
        assert rd[2] == journal.cursor and rd[3] == journal.ledger.head and len(rd[4]) == 64
        inv = rows(raw, "invocations")[1]
        assert inv[4] == "read" and inv[1] is None and inv[7] is not None
        assert count(raw, "decisions") == 1  # the write's; the null policy gates no reads
        assert count(raw, "operations") == 1 and count(raw, "outcomes") == 1

    def test_read_of_unknown_account_is_an_admission_failure(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        r = journal.handle(balance("nope"))
        assert (r.disposition, r.response, r.ok) == ("invalid", "invalid", False)
        assert r.error_message == "unknown_account at arguments.account"
        assert count(raw, "reads") == 0 and count(raw, "invocation_responses") == 1

    def test_trial_balance_amounts_are_decimal_strings(self, journal: Journal) -> None:
        journal.handle(post("k1"))
        r = journal.handle({"tool": "trial_balance", "call_id": "tb", "arguments": {}})
        assert r.ok and r.result["balanced"] is True
        cash = next(row for row in r.result["rows"] if row["account"] == "cash")
        assert cash["debit"] == {"amount": "1999", "currency": "USD"}
        assert all(isinstance(row["debit"]["amount"], str) for row in r.result["rows"])

    def test_denied_gated_read_writes_no_reads_row(
        self, journal_path: str, raw: sqlite3.Connection
    ) -> None:
        class GateReads(NullPolicySet):
            def gates_read(self, tool: str) -> bool:
                return tool == "balance"

            def evaluate(self, context: PolicyContext) -> Decision:
                if context.digest_kind == "request":
                    return Decision("deny", "test.no_reads", "reads are gated in this test")
                return Decision("allow", "none.allow_all", "x")

        j = Journal.create(
            journal_path, CHART, clock=SteppingClock(EPOCH), ids=SequentialIds(), policy=GateReads()
        )
        r = j.handle(balance("cash"))
        assert (r.disposition, r.response, r.ok, r.error_type) == (
            "read",
            "denied",
            False,
            "PolicyDenied",
        )
        assert count(raw, "reads") == 0
        (dec,) = rows(raw, "decisions")
        assert (
            dec[2] is None and dec[5] == "deny" and json.loads(dec[3])["digest_kind"] == "request"
        )
        (resp,) = rows(raw, "invocation_responses")
        assert resp[3] is None and resp[4] == "denied"
        j.close()


class TestCursorAndRebuild:  # family 6
    def test_lifecycle_only_outcome_advances_cursor_with_head_unchanged(
        self, journal: Journal
    ) -> None:
        head = journal.ledger.head
        before = journal.cursor
        journal.handle(open_txn("k1", "t1"))
        assert journal.ledger.head == head and journal.cursor > before

    def test_rejected_outcome_advances_cursor(self, journal: Journal) -> None:
        before = journal.cursor
        journal.handle(
            {
                "tool": "refund",
                "call_id": "c",
                "key": "k",
                "arguments": {"transaction_id": "none", "money": {"amount": 1, "currency": "USD"}},
            }
        )
        assert journal.cursor > before

    def test_reopen_rebuilds_the_same_projection(
        self, journal: Journal, reopen: Callable[[], Journal]
    ) -> None:
        journal.handle(post("k1"))
        journal.handle(open_txn("k2", "t1"))
        journal.handle(post("k3", amount=5))
        head, cursor, seq = journal.ledger.head, journal.cursor, journal.ledger.sequence
        journal.close()
        again = reopen()
        assert (again.ledger.head, again.cursor, again.ledger.sequence) == (head, cursor, seq)
        assert again.ledger.transaction("t1").amount.amount == 1000
        again.close()

    def test_stale_process_rebuilds_before_evaluating(
        self, journal: Journal, journal_path: str
    ) -> None:
        """Two processes on one journal: the second cannot apply against a stale view."""
        other = Journal.open(journal_path, clock=SteppingClock(EPOCH), ids=SequentialIds(start=500))
        journal.handle(post("k1"))  # first process advances the journal
        assert other.cursor < journal.cursor
        r = other.handle(
            post("k1", call_id="from-other")
        )  # same key: must be a replay, not a new apply
        assert r.response == "replayed" and other.cursor == journal.cursor
        assert other.ledger.head == journal.ledger.head
        other.close()

    def test_empty_journal_is_current_at_cursor_zero(
        self, journal: Journal, reopen: Callable[[], Journal]
    ) -> None:
        assert journal.cursor == 0
        journal.close()
        again = reopen()
        assert again.cursor == 0 and again.ledger.sequence == 0
        again.close()

    def test_rebuild_detects_a_tampered_head(self, journal: Journal, journal_path: str) -> None:
        journal.handle(post("k1"))
        journal.close()
        conn = sqlite3.connect(journal_path, isolation_level=None)
        conn.execute("DROP TRIGGER outcomes_no_update")  # simulate an attacker with file access
        conn.execute("UPDATE outcomes SET head_after = ?", ("f" * 64,))
        conn.close()
        with pytest.raises(JournalError, match="head"):
            Journal.open(journal_path, clock=SteppingClock(EPOCH), ids=SequentialIds())


class TestOutcomeChainConstraints:  # family 8
    def _op(self, raw: sqlite3.Connection) -> int:
        return int(rows(raw, "operations")[0][0])

    def _alloc(self, raw: sqlite3.Connection, kind: str) -> int:
        rowid = raw.execute("INSERT INTO journal (kind) VALUES (?)", (kind,)).lastrowid
        assert rowid is not None
        return int(rowid)

    def _outcome(
        self,
        raw: sqlite3.Connection,
        op: int,
        previous: int | None,
        kind: str = "awaiting_approval",
    ) -> int:
        seq = self._alloc(raw, "outcomes")
        raw.execute(
            "INSERT INTO outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (seq, op, previous, kind, None, None, None, None, "h", "h", 0, None),
        )
        return seq

    def test_second_root_is_rejected(self, journal: Journal, raw: sqlite3.Connection) -> None:
        journal.handle(post("k1"))
        raw.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            self._outcome(raw, self._op(raw), None)
        raw.execute("ROLLBACK")

    def test_successor_of_terminal_outcome_is_rejected(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        journal.handle(post("k1"))  # applied, terminal
        (out,) = rows(raw, "outcomes")
        raw.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError, match="only awaiting_approval"):
            self._outcome(raw, self._op(raw), out[0])
        raw.execute("ROLLBACK")

    def test_fork_and_cross_operation_predecessor_are_rejected(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        journal.handle(open_txn("k1", "t1"))
        journal.handle(open_txn("k2", "t2"))
        op1, op2 = (r[0] for r in rows(raw, "operations"))
        raw.execute("BEGIN")
        # a pending root for op1, appended by hand, to have something with successors
        root = self._alloc(raw, "operations")
        raw.execute("INSERT INTO operations VALUES (?,?,?,?)", (root, "hand", "fp", "{}"))
        pending = self._outcome(raw, root, None)
        first = self._outcome(raw, root, pending)  # legal successor
        with pytest.raises(sqlite3.IntegrityError):  # fork: second successor of `pending`
            self._outcome(raw, root, pending)
        raw.execute("ROLLBACK")
        raw.execute("BEGIN")
        root = self._alloc(raw, "operations")
        raw.execute("INSERT INTO operations VALUES (?,?,?,?)", (root, "hand2", "fp", "{}"))
        pending = self._outcome(raw, root, None)
        with pytest.raises(sqlite3.IntegrityError):  # predecessor belongs to another operation
            self._outcome(raw, op2, pending)
        raw.execute("ROLLBACK")
        del first, op1


class TestResponseShape:  # family 1 (FK order) and the CHECK on responses
    def test_response_outcome_nullability_follows_disposition(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        journal.handle(post("k1"))
        journal.handle(post("k1", call_id="r"))
        journal.handle(post("k1", call_id="c", amount=1))
        journal.handle(balance("cash"))
        journal.handle("junk")
        by_disp = {r[2]: r[3] for r in rows(raw, "invocation_responses")}
        assert by_disp["new"] is not None and by_disp["replay"] is not None
        assert (
            by_disp["conflict"] is None and by_disp["read"] is None and by_disp["invalid"] is None
        )

    def test_response_row_must_match_its_invocation_disposition(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        journal.handle(balance("cash"))
        (inv,) = rows(raw, "invocations")
        raw.execute("BEGIN")
        seq = raw.execute("INSERT INTO journal (kind) VALUES ('invocation_responses')").lastrowid
        with pytest.raises(sqlite3.IntegrityError):  # UNIQUE(invocation) or the disposition trigger
            raw.execute(
                "INSERT INTO invocation_responses VALUES (?,?,?,?,?)",
                (seq, inv[0], "new", None, "applied"),
            )
        raw.execute("ROLLBACK")

    def test_reference_before_target_is_refused_by_foreign_keys(
        self, raw: sqlite3.Connection, journal: Journal
    ) -> None:
        raw.execute("BEGIN")
        seq = raw.execute("INSERT INTO journal (kind) VALUES ('events')").lastrowid
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("INSERT INTO events VALUES (?, 999999, 'inbound', '{}')", (seq,))
        raw.execute("ROLLBACK")


class TestAtomicity:  # family 11
    def test_a_failure_inside_the_transaction_leaves_no_rows(
        self, journal: Journal, raw: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = count(raw, "journal")

        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("simulated crash before commit")

        monkeypatch.setattr(journal, "_respond", boom)
        with pytest.raises(RuntimeError):
            journal.handle(post("k1"))
        assert count(raw, "journal") == before and count(raw, "operations") == 0
        assert journal.ledger.sequence == 0 and journal.cursor == 0  # projection unchanged
        monkeypatch.undo()
        r = journal.handle(post("k1"))  # the retry runs afresh
        assert r.response == "applied"

    def test_message_events_are_their_own_transaction(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        seq = journal.record_message("user", "refund order 42")
        (ev,) = rows(raw, "events")
        assert ev[0] == seq and ev[1] is None and ev[2] == "message"


class TestDefinition:
    def test_definition_is_written_once_and_binds_the_policy_version(
        self, journal: Journal, journal_path: str, raw: sqlite3.Connection
    ) -> None:
        (d,) = rows(raw, "definition")
        assert d[1] == 1 and len(d[2]) == 32 and d[5] == "none" and d[8] == "none"
        journal.close()
        with pytest.raises(JournalError, match="already has a definition"):
            Journal.create(journal_path, CHART, clock=SteppingClock(EPOCH), ids=SequentialIds())

        class Other(NullPolicySet):
            version = "other"

        with pytest.raises(ConfigurationError, match="policy set"):
            Journal.open(
                journal_path, clock=SteppingClock(EPOCH), ids=SequentialIds(), policy=Other()
            )

    def test_open_never_manufactures_a_journal(self, tmp_path: object) -> None:
        missing = f"{tmp_path}/empty.journal"
        with pytest.raises(JournalError, match="cannot open"):
            Journal.open(missing, clock=SteppingClock(EPOCH), ids=SequentialIds())
        assert not Path(missing).exists()
        empty = f"{tmp_path}/schema-only.journal"
        sqlite3.connect(empty).close()  # a file that is a database but not a journal
        with pytest.raises(JournalError, match=r"not a journal|no definition"):
            Journal.open(empty, clock=SteppingClock(EPOCH), ids=SequentialIds())
        not_db = f"{tmp_path}/text.journal"
        Path(not_db).write_text("hello")
        with pytest.raises(JournalError, match=r"not a database|not a journal"):
            Journal.open(not_db, clock=SteppingClock(EPOCH), ids=SequentialIds())


class TestDecisionToOutcome:
    """The new-operation table, driven by a test policy set. Real sets arrive in M3."""

    class Rules(NullPolicySet):
        version = "none"  # same version so the definition binds; behaviour differs for the test

        def evaluate(self, context: PolicyContext) -> Decision:
            if context.command_digest.startswith("0"):
                return Decision("deny", "test.zero_prefix", "digests starting with 0 are refused")
            if context.command_digest.startswith("1"):
                return Decision("approval_required", "test.one_prefix", "needs a human")
            return Decision("allow", "test.allow", "fine")

    def _journal_with(self, path: str) -> Journal:
        return Journal.create(
            path, CHART, clock=SteppingClock(EPOCH), ids=SequentialIds(), policy=self.Rules()
        )

    def _first_with_prefix(self, prefix: str) -> dict[str, object]:
        from ledgergate.codec import decode_command
        from ledgergate.ledger import CURRENCIES, command_fingerprint

        for i in range(1, 500):
            req = post(f"key-{i}", amount=i)  # the fingerprint covers the draft, not the key
            doc = {"kind": "post", "key": f"key-{i}", **req["arguments"]}
            if command_fingerprint(decode_command(doc, CURRENCIES)).startswith(prefix):
                return req
        raise AssertionError("no fingerprint with that prefix in 500 tries")

    def test_deny_appends_a_terminal_denied_outcome(
        self, journal_path: str, raw: sqlite3.Connection
    ) -> None:
        j = self._journal_with(journal_path)
        req = self._first_with_prefix("0")
        r = j.handle(req)
        assert (r.response, r.ok, r.error_type) == ("denied", False, "PolicyDenied")
        (out,) = rows(raw, "outcomes")
        assert out[3] == "denied" and out[8] == out[9] and j.ledger.sequence == 0
        again = j.handle({**req, "call_id": "again"})
        assert again.response == "replayed" and again.error_type == "PolicyDenied"
        assert count(raw, "outcomes") == 1
        j.close()

    def test_approval_required_appends_a_pending_outcome_and_replays_it(
        self, journal_path: str, raw: sqlite3.Connection
    ) -> None:
        j = self._journal_with(journal_path)
        req = self._first_with_prefix("1")
        r = j.handle(req)
        assert (r.response, r.ok, r.error_type) == ("awaiting_approval", False, "ApprovalRequired")
        (out,) = rows(raw, "outcomes")
        assert out[3] == "awaiting_approval"
        again = j.handle({**req, "call_id": "again"})  # no approval presented: replay of pending
        assert again.response == "replayed" and again.error_type == "ApprovalRequired"
        assert count(raw, "outcomes") == 1 and j.cursor == out[0]
        j.close()


class TestAdmissionEdges:
    @pytest.mark.parametrize(
        ("value", "code", "path"),
        [
            ({"call_id": "c", "key": "k", "arguments": {}}, "missing_field", "tool"),
            ({"tool": 5, "call_id": "c", "key": "k", "arguments": {}}, "wrong_type", "tool"),
            (
                {"tool": "post", "call_id": "c", "key": "k", "arguments": []},
                "wrong_type",
                "arguments",
            ),
            ({"tool": "post", "call_id": "c", "arguments": {}}, "missing_field", "key"),
            (
                {"tool": "post", "call_id": "c", "key": "k", "arguments": {"kind": "post"}},
                "unexpected_field",
                "arguments.kind",
            ),
            (
                {"tool": "post", "call_id": "c", "key": " k", "arguments": {}},
                "invalid_identifier",
                "key",
            ),
        ],
    )
    def test_shape_errors_name_the_field_and_carry_no_values(
        self, journal: Journal, value: object, code: str, path: str
    ) -> None:
        r = journal.handle(value)
        assert r.response == "invalid" and r.error_message == f"{code} at {path}"
        assert "k" not in (r.error_message or "").split(" at ")[0]  # the code, not the value


class TestReviewFindings:
    """Regressions for the review of the first M2b implementation."""

    @pytest.mark.parametrize(
        ("postings", "error"),
        [
            ([("cash", "debit", 2), ("revenue", "credit", 1)], "UnbalancedEntryError"),
            ([("cash", "debit", 0), ("revenue", "credit", 0)], "InvalidAmountError"),
            ([("cash", "debit", -5), ("revenue", "credit", -5)], "InvalidAmountError"),
        ],
    )
    def test_commands_the_core_constructors_reject_are_recorded_as_invalid(
        self,
        journal: Journal,
        raw: sqlite3.Connection,
        postings: list[tuple[str, str, int]],
        error: str,
    ) -> None:
        """An unbalanced or non-positive draft must not escape the journal unrecorded."""
        draft = {
            "postings": [
                {"account": a, "side": s, "money": {"amount": n, "currency": "USD"}}
                for a, s, n in postings
            ]
        }
        r = journal.handle(
            {"tool": "post", "call_id": "c", "key": "k", "arguments": {"draft": draft}}
        )
        assert (r.disposition, r.response) == ("invalid", "invalid")
        assert r.error_message == f"malformed_command:{error} at arguments"
        assert count(raw, "invocations") == 1 and count(raw, "operations") == 0
        # and the key is not spent by a malformed attempt
        ok = journal.handle(post("k", call_id="c2"))
        assert ok.response == "applied"

    def test_envelope_payload_is_bounded_in_bytes_not_characters(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        journal.handle({"tool": "post", "call_id": "c", "key": "k", "arguments": {"x": "€" * 5000}})
        inbound = rows(raw, "events")[0]
        payload = json.loads(inbound[3])["payload"]
        assert len(payload.encode("utf-8")) <= 4096

    def test_replay_answers_exactly_what_the_first_invocation_was_told(
        self, journal: Journal
    ) -> None:
        first = journal.handle(open_txn("k1", "t1"))
        again = journal.handle({**open_txn("k1", "t1"), "call_id": "again"})
        assert {**first.result, "replayed": True} == again.result  # transaction, head, sequence
        assert again.result["transaction"] == {"id": "t1", "status": "pending"}

    def test_replay_of_a_rejection_answers_the_original_error(self, journal: Journal) -> None:
        req = {
            "tool": "refund",
            "call_id": "c",
            "key": "k",
            "arguments": {"transaction_id": "ghost", "money": {"amount": 1, "currency": "USD"}},
        }
        first = journal.handle(req)
        again = journal.handle({**req, "call_id": "c2"})
        assert (again.error_type, again.error_message) == (first.error_type, first.error_message)

    def test_non_i_json_input_is_refused_before_any_row(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        before = count(raw, "journal")
        with pytest.raises(JournalError, match="not I-JSON"):
            journal.handle({"tool": "post", "call_id": "c", "key": "k", "arguments": {"n": 2**60}})
        with pytest.raises(JournalError, match="not I-JSON"):
            journal.handle(
                {"tool": "post", "call_id": "c", "key": "k", "arguments": {"f": float("nan")}}
            )
        assert count(raw, "journal") == before

    def test_chain_self_check_refuses_a_broken_journal(
        self, journal: Journal, journal_path: str
    ) -> None:
        journal.handle(open_txn("k1", "t1"))
        journal.close()
        conn = sqlite3.connect(journal_path, isolation_level=None)
        conn.execute("PRAGMA foreign_keys = OFF")
        for trig in ("outcomes_no_update", "outcomes_no_insert"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
        conn.execute("DROP TRIGGER outcomes_successor_of_pending")
        (op,) = conn.execute("SELECT journal_sequence FROM operations").fetchone()
        (tip,) = conn.execute("SELECT MAX(journal_sequence) FROM outcomes").fetchone()
        seq = conn.execute("INSERT INTO journal (kind) VALUES ('outcomes')").lastrowid
        conn.execute(  # a successor of a terminal (applied) outcome: forbidden by the chain rules
            "INSERT INTO outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (seq, op, tip, "denied", None, None, None, None, "h", "h", 0, None),
        )
        conn.close()
        with pytest.raises(JournalError, match="chain"):
            Journal.open(journal_path, clock=SteppingClock(EPOCH), ids=SequentialIds())

    def test_commit_failure_rolls_back_and_leaves_a_usable_connection(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        # Hold a write lock from another connection so the journal's transaction cannot start.
        journal._conn.execute("PRAGMA busy_timeout = 50")  # keep the test fast
        raw.execute("BEGIN IMMEDIATE")
        with pytest.raises(JournalError, match="unavailable"):
            journal.handle(post("k1"))
        raw.execute("ROLLBACK")
        assert journal.handle(post("k1")).response == "applied"  # the connection recovered

    def test_open_refuses_a_journal_from_another_schema_or_codec_version(
        self, journal: Journal, journal_path: str
    ) -> None:
        journal.close()
        conn = sqlite3.connect(journal_path, isolation_level=None)
        conn.execute("DROP TRIGGER definition_no_update")
        conn.execute("UPDATE definition SET codec_version = '99'")
        conn.close()
        with pytest.raises(ConfigurationError, match="codec"):
            Journal.open(journal_path, clock=SteppingClock(EPOCH), ids=SequentialIds())

    def test_definition_is_a_singleton_by_constraint(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        (d,) = rows(raw, "definition")
        raw.execute("BEGIN")
        seq = raw.execute("INSERT INTO journal (kind) VALUES ('definition')").lastrowid
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("INSERT INTO definition VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (seq, *d[1:]))
        raw.execute("ROLLBACK")

    def test_request_digest_covers_the_principal(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        journal.handle(balance("cash"))
        (inv,) = rows(raw, "invocations")
        from ledgergate.codec import digest

        expected = digest(
            {
                "tool": "balance",
                "arguments": {"account": "cash"},
                "call_id": "call-read",
                "principal": "local",
            }
        )
        assert inv[7] == expected


class TestEffectFaults:
    """A fault of this process's clock or id generator is never a verdict on the command."""

    def test_stale_id_generator_in_a_second_instance_is_unrecorded_and_spends_no_key(
        self, journal: Journal, journal_path: str, raw: sqlite3.Connection
    ) -> None:
        journal.handle(post("k1"))  # produces e-000001
        other = Journal.open(journal_path, clock=SteppingClock(EPOCH), ids=SequentialIds())  # stale
        before = count(raw, "journal")
        with pytest.raises(EffectError, match="fresh across processes"):
            other.handle(post("k2"))
        assert count(raw, "journal") == before and count(raw, "operations") == 1
        fresh = Journal.open(journal_path, clock=SteppingClock(EPOCH), ids=SequentialIds(start=2))
        assert fresh.handle(post("k2")).response == "applied"  # the key was never spent
        other.close()
        fresh.close()

    def test_naive_clock_is_an_effect_fault_on_every_path(
        self, journal_path: str, tmp_path: object
    ) -> None:
        """Not only when an entry is posted: every timestamp the journal stores comes from
        the clock, and a naive one would be read in the host's zone."""
        from datetime import datetime

        class Naive:
            def now(self) -> datetime:
                return datetime(2026, 1, 1)  # noqa: DTZ001 - the naive case is the point

        with pytest.raises(EffectError, match="naive"):  # definition.created_at
            Journal.create(f"{tmp_path}/naive.journal", CHART, clock=Naive(), ids=SequentialIds())

        good = Journal.create(journal_path, CHART, clock=SteppingClock(EPOCH), ids=SequentialIds())
        good.close()
        j = Journal.open(journal_path, clock=Naive(), ids=SequentialIds())
        for request in (
            post("k1"),  # posts an entry
            open_txn("k2", "t2"),  # lifecycle only, no entry
            balance("cash"),  # read
            "junk",  # invalid
        ):
            with pytest.raises(EffectError, match="naive"):
                j.handle(request)
        raw = sqlite3.connect(journal_path)
        assert raw.execute("SELECT COUNT(*) FROM invocations").fetchone()[0] == 0
        raw.close()
        j.close()

    def test_invalid_generated_id_is_an_effect_fault(self, journal_path: str) -> None:
        class Bad:
            def next_id(self) -> str:
                return "two\nlines"

        j = Journal.create(journal_path, CHART, clock=SteppingClock(EPOCH), ids=Bad())
        with pytest.raises(EffectError, match="invalid id"):
            j.handle(post("k1"))
        j.close()


class TestRedactionSeam:
    class Loud(IdentityAdmitter):
        def redact_text(self, text: str) -> str:
            return "[redacted]"

    def test_redact_text_is_called_at_every_free_text_site(
        self, journal_path: str, raw: sqlite3.Connection
    ) -> None:
        chart = ChartOfAccounts([Account("cash", AccountType.ASSET, USD, name="Alice's wallet")])
        j = Journal.create(
            journal_path,
            chart,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            admitter=self.Loud(),
        )
        j.record_message("user", "my card is 4111")
        j.handle(
            {
                "tool": "refund",
                "call_id": "c",
                "key": "k",
                "arguments": {
                    "transaction_id": "cust@example.com",
                    "money": {"amount": 1, "currency": "USD"},
                },
            }
        )
        (d,) = rows(raw, "definition")
        assert json.loads(d[9])[0]["name"] == "[redacted]"
        message = next(e for e in rows(raw, "events") if e[2] == "message")
        assert json.loads(message[3])["content"] == "[redacted]"
        (out,) = rows(raw, "outcomes")
        assert out[3] == "rejected" and out[5] == "[redacted]"  # the core's message, redacted
        j.close()

    def test_failure_envelope_goes_through_the_seam(
        self, journal_path: str, raw: sqlite3.Connection
    ) -> None:
        class Tokenizing(IdentityAdmitter):
            def redact_text(self, text: str) -> str:
                return "[redacted]"

            def tokenize_identifier(self, value: str) -> str:
                return "tok_" + str(len(value))

        j = Journal.create(
            journal_path,
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            admitter=Tokenizing(),
        )
        j.handle(
            {
                "tool": "post",
                "call_id": "cust@example.com",
                "key": "k",
                "arguments": {"card": "4111"},
            }
        )
        j.handle({"tool": "post", "call_id": "c2", "key": "k", "arguments": {}, "SSN 123-45": 1})
        first, _second = rows(raw, "invocations")
        assert first[8] == "tok_16"
        envelope = json.loads(rows(raw, "events")[0][3])
        assert envelope["call_id"] == "tok_16" and envelope["payload"] == "[redacted]"
        assert "4111" not in json.dumps(envelope)
        # an unknown member name is the caller's: it appears in no row
        everything = " ".join(json.dumps(r, default=str) for r in rows(raw, "events"))
        assert "SSN" not in everything and "123-45" not in everything
        assert json.loads(rows(raw, "events")[2][3])["error"] == {
            "code": "unknown_field",
            "path": "$",
        }
        j.close()

    def test_open_refuses_an_admitter_with_a_different_token_key(
        self, journal: Journal, journal_path: str
    ) -> None:
        class Other(IdentityAdmitter):
            token_key_version = "v2"

        journal.close()
        with pytest.raises(ConfigurationError, match="tokens"):
            Journal.open(
                journal_path, clock=SteppingClock(EPOCH), ids=SequentialIds(), admitter=Other()
            )


class TestCreateHardening:
    def test_create_refuses_a_foreign_database_and_a_non_database(self, tmp_path: Path) -> None:
        foreign = str(tmp_path / "foreign.sqlite")
        conn = sqlite3.connect(foreign)
        conn.execute("CREATE TABLE customers (id INTEGER)")
        conn.close()
        before = Path(foreign).read_bytes()
        with pytest.raises(JournalError, match="not a journal"):
            Journal.create(foreign, CHART, clock=SteppingClock(EPOCH), ids=SequentialIds())
        assert Path(foreign).read_bytes() == before  # refused byte-for-byte unchanged
        text = tmp_path / "text.sqlite"
        text.write_text("hello")
        with pytest.raises(JournalError, match="cannot create"):
            Journal.create(str(text), CHART, clock=SteppingClock(EPOCH), ids=SequentialIds())

    def test_input_digest_goes_through_the_admitter(
        self, journal_path: str, raw: sqlite3.Connection
    ) -> None:
        class Keyed(IdentityAdmitter):
            def digest_input(self, value: object) -> str:
                return "keyed:" + "0" * 58

        j = Journal.create(
            journal_path, CHART, clock=SteppingClock(EPOCH), ids=SequentialIds(), admitter=Keyed()
        )
        j.handle("junk")
        envelope = json.loads(rows(raw, "events")[0][3])
        assert envelope["input_digest"].startswith("keyed:")
        j.close()


class TestIdentifiersInsideArguments:
    @pytest.mark.parametrize(
        ("tool", "arguments", "path"),
        [
            (
                "open_transaction",
                {"transaction_id": "a\nb", "amount": {"amount": 1, "currency": "USD"}},
                "arguments.transaction_id",
            ),
            (
                "advance",
                {"transaction_id": " padded", "event": "authorize"},
                "arguments.transaction_id",
            ),
            (
                "refund",
                {"transaction_id": "x" * 300, "money": {"amount": 1, "currency": "USD"}},
                "arguments.transaction_id",
            ),
            ("reverse", {"entry_id": ""}, "arguments.entry_id"),
        ],
    )
    def test_invalid_caller_identifier_in_arguments_is_an_admission_failure(
        self,
        journal: Journal,
        raw: sqlite3.Connection,
        tool: str,
        arguments: dict[str, object],
        path: str,
    ) -> None:
        r = journal.handle({"tool": tool, "call_id": "c", "key": "k", "arguments": arguments})
        assert (r.disposition, r.response) == ("invalid", "invalid")
        assert r.error_message == f"invalid_identifier at {path}"
        assert count(raw, "operations") == 0
        assert journal.handle(open_txn("k", "t-ok")).response == "applied"  # key not spent


class TestRegistryBinding:
    def test_a_later_bundle_does_not_leak_into_an_old_journal(
        self, journal: Journal, journal_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ledgergate import ledger as ledger_pkg
        from ledgergate.journal import store as store_mod
        from ledgergate.ledger import Currency

        journal.close()
        newer = {**ledger_pkg.CURRENCIES, "ZZZ": Currency("ZZZ", 2)}
        monkeypatch.setattr(store_mod, "CURRENCIES", newer)
        j = Journal.open(journal_path, clock=SteppingClock(EPOCH), ids=SequentialIds())
        assert "ZZZ" not in j.definition.registry
        r = j.handle(
            {
                "tool": "open_transaction",
                "call_id": "c",
                "key": "k",
                "arguments": {"transaction_id": "t", "amount": {"amount": 1, "currency": "ZZZ"}},
            }
        )
        assert r.response == "invalid" and "malformed_command" in (r.error_message or "")
        j.close()

    def test_undecodable_applied_outcome_is_an_integrity_failure(
        self, journal: Journal, journal_path: str
    ) -> None:
        journal.handle(open_txn("k1", "t1"))
        journal.close()
        conn = sqlite3.connect(journal_path, isolation_level=None)
        conn.execute("DROP TRIGGER operations_no_update")
        conn.execute('UPDATE operations SET command = \'{"kind": "teleport"}\'')
        conn.close()
        with pytest.raises(JournalError, match="does not decode"):
            Journal.open(journal_path, clock=SteppingClock(EPOCH), ids=SequentialIds())

    def test_open_does_not_touch_a_foreign_file(self, tmp_path: Path) -> None:
        foreign = tmp_path / "foreign.sqlite"
        conn = sqlite3.connect(str(foreign))
        conn.execute("CREATE TABLE customers (id INTEGER)")
        conn.close()
        before = foreign.read_bytes()
        with pytest.raises(JournalError, match="not a journal"):
            Journal.open(str(foreign), clock=SteppingClock(EPOCH), ids=SequentialIds())
        assert foreign.read_bytes() == before  # no WAL switch, no schema, nothing

    def test_write_to_unknown_account_is_an_admission_failure(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        draft = {
            "postings": [
                {"account": "nope", "side": "debit", "money": {"amount": 1, "currency": "USD"}},
                {"account": "revenue", "side": "credit", "money": {"amount": 1, "currency": "USD"}},
            ]
        }
        r = journal.handle(
            {"tool": "post", "call_id": "c", "key": "k", "arguments": {"draft": draft}}
        )
        assert r.response == "invalid"
        assert r.error_message == "unknown_account at arguments.draft.postings[0].account"
        assert count(raw, "operations") == 0
        assert journal.handle(post("k")).response == "applied"  # the key was never spent

    def test_corrupt_definition_is_an_integrity_failure(
        self, journal: Journal, journal_path: str
    ) -> None:
        journal.close()
        conn = sqlite3.connect(journal_path, isolation_level=None)
        conn.execute("DROP TRIGGER definition_no_update")
        conn.execute("UPDATE definition SET chart = 'not json'")
        conn.close()
        with pytest.raises(JournalError, match="definition does not decode"):
            Journal.open(journal_path, clock=SteppingClock(EPOCH), ids=SequentialIds())

    def test_message_role_is_constrained(self, journal: Journal) -> None:
        with pytest.raises(ValueError, match="role"):
            journal.record_message("cust@example.com", "hi")
        assert journal.record_message("assistant", "hi") > 0
