"""The journal's write and read protocols, tested family by family against the spec.

Each class below corresponds to one of the test families the specification implies.
Assertions are made against the rows in the database, not only the returned response,
because the rows are the record.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

import pytest
from tests.unit.journal.support import CHART, balance, count, open_txn, post, rows

from ledgergate.journal import (
    FACT_TABLES,
    ConfigurationError,
    Journal,
    JournalError,
    NullPolicySet,
)
from ledgergate.journal.policy import Decision, PolicyContext
from ledgergate.ledger import EPOCH, SequentialIds, SteppingClock


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
                "command(post).draft",
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
                "bogus",
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

    def test_read_of_unknown_account_is_a_recorded_failure_not_a_crash(
        self, journal: Journal, raw: sqlite3.Connection
    ) -> None:
        r = journal.handle(balance("nope"))
        assert (r.response, r.ok, r.error_type) == ("read", False, "UnknownAccountError")
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
        assert len(d[1]) == 32 and d[4] == "none" and d[7] == "none"
        journal.close()
        with pytest.raises(JournalError, match="already has a definition"):
            Journal.create(journal_path, CHART, clock=SteppingClock(EPOCH), ids=SequentialIds())

        class Other(NullPolicySet):
            version = "other"

        with pytest.raises(ConfigurationError, match="policy set"):
            Journal.open(
                journal_path, clock=SteppingClock(EPOCH), ids=SequentialIds(), policy=Other()
            )

    def test_open_without_definition_fails(self, tmp_path: object) -> None:
        with pytest.raises(JournalError, match="no definition"):
            Journal.open(
                f"{tmp_path}/empty.journal", clock=SteppingClock(EPOCH), ids=SequentialIds()
            )


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
