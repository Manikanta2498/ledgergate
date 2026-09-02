"""Recording a session into a trace, and replaying a trace against the core."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ledgergate.ledger import (
    EPOCH,
    KWD,
    USD,
    Account,
    AccountType,
    Advance,
    ChartOfAccounts,
    EntryDraft,
    IllegalTransitionError,
    InvalidAmountError,
    Money,
    OpenTransaction,
    Post,
    Refund,
    SequentialIds,
    SteppingClock,
    TransactionEvent,
    UnknownTransactionError,
    credit,
    debit,
)
from ledgergate.trace import (
    AgentDoc,
    LedgerCommandEvent,
    LedgerResultEvent,
    Recorder,
    TraceError,
    dump_trace,
    parse_trace,
    replay_trace,
)

E = TransactionEvent
CHART = ChartOfAccounts(
    [Account("cash", AccountType.ASSET, USD), Account("revenue", AccountType.REVENUE, USD)]
)


def sale(amount: int = 1000) -> EntryDraft:
    return EntryDraft.of(debit("cash", Money(amount, USD)), credit("revenue", Money(amount, USD)))


def recorder() -> Recorder:
    return Recorder("t", AgentDoc(name="a"), CHART, SteppingClock(EPOCH), SequentialIds())


def settled(rec: Recorder) -> None:
    rec.execute(OpenTransaction("o", "t", Money(1000, USD)))
    rec.execute(Advance("a", "t", E.AUTHORIZE))
    rec.execute(Advance("s", "t", E.SETTLE, sale()))


class TestRecorder:
    def test_events_are_sequenced_and_paired(self) -> None:
        rec = recorder()
        rec.message("user", "hi")
        rec.tool_call("c1", "open", {"x": 1}, idempotency_key="o")
        rec.execute(OpenTransaction("o", "t", Money(1, USD)), call_id="c1")
        rec.tool_result("c1", True, {"ok": 1})
        trace = rec.trace()
        assert [e.seq for e in trace.events] == [1, 2, 3, 4, 5]
        cmd = trace.events[2]
        res = trace.events[3]
        assert isinstance(cmd, LedgerCommandEvent) and cmd.call_id == "c1"
        assert isinstance(res, LedgerResultEvent) and res.command_id == cmd.command_id
        assert res.ok and res.replayed is False and res.sequence == 0 and res.entry_id is None

    def test_appended_entry_effects_are_recorded(self) -> None:
        rec = recorder()
        applied = rec.execute(Post("p", sale()))
        res = rec.trace().events[-1]
        assert isinstance(res, LedgerResultEvent) and applied.entry is not None
        assert res.entry_id == applied.entry.entry_id == "e-000001"
        assert res.posted_at == applied.entry.posted_at
        assert res.head == applied.ledger.head and res.sequence == 1

    def test_replayed_command_records_no_new_effects(self) -> None:
        rec = recorder()
        rec.execute(Post("p", sale()))
        rec.execute(Post("p", sale()))
        res = rec.trace().events[-1]
        assert isinstance(res, LedgerResultEvent)
        assert res.replayed is True and res.entry_id is None and res.sequence == 1

    def test_failure_is_recorded_then_reraised_and_ledger_unchanged(self) -> None:
        rec = recorder()
        rec.execute(OpenTransaction("o", "t", Money(1000, USD)))
        head = rec.ledger.head
        with pytest.raises(IllegalTransitionError):
            rec.execute(Refund("r", "t", Money(1, USD), sale(1)))
        res = rec.trace().events[-1]
        assert isinstance(res, LedgerResultEvent)
        assert res.ok is False and res.error is not None
        assert res.error.type == "IllegalTransitionError"
        assert res.head == head and rec.ledger.head == head

    def test_financially_invalid_attempt_is_recorded_and_replays(self) -> None:
        """A zero-amount open is exactly the kind of attempt a trace must preserve."""
        rec = recorder()
        with pytest.raises(InvalidAmountError):
            rec.execute(OpenTransaction("o", "t", Money(0, USD)))
        assert len(rec.events) == 2
        res = rec.events[-1]
        assert isinstance(res, LedgerResultEvent) and res.ok is False
        assert res.error is not None and res.error.type == "InvalidAmountError"
        assert replay_trace(rec.trace()).consistent

    def test_currencies_are_declared_with_exponents(self) -> None:
        rec = recorder()
        kwd_chart_free = Refund("r", "t", Money(1, KWD))  # currency not in the chart
        with pytest.raises(UnknownTransactionError):
            rec.execute(kwd_chart_free)
        trace = rec.trace()
        assert trace.currencies is not None
        assert {c.code: c.exponent for c in trace.currencies} == {"USD": 2, "KWD": 3}

    def test_run_tolerates_errors_and_records_all(self) -> None:
        rec = recorder()
        rec.run(
            [
                OpenTransaction("o", "t", Money(1, USD)),
                Advance("bad", "t", E.SETTLE),
                Advance("a", "t", E.AUTHORIZE),
            ]
        )
        results = [e for e in rec.trace().events if isinstance(e, LedgerResultEvent)]
        assert [r.ok for r in results] == [True, False, True]

    def test_trace_carries_chart_and_metadata(self) -> None:
        rec = Recorder(
            "t",
            AgentDoc(name="a", model="m"),
            CHART,
            SteppingClock(EPOCH),
            SequentialIds(),
            scenario_id="s",
            metadata={"k": "v"},
        )
        trace = rec.trace()
        assert trace.scenario_id == "s" and trace.metadata == {"k": "v"}
        assert trace.chart_of_accounts()["cash"] == CHART["cash"]
        assert trace.ended_at is not None and trace.ended_at > trace.started_at


class TestReplay:
    def test_faithful_trace_replays_consistently(self) -> None:
        rec = recorder()
        settled(rec)
        refund = Refund("r", "t", Money(300, USD), sale(300).reversed())
        rec.execute(refund)
        rec.execute(refund)
        report = replay_trace(rec.trace())
        assert report.consistent and report.commands_replayed == 5
        assert report.ledger.head == rec.ledger.head
        assert report.ledger.balance("cash") == Money(700, USD)

    def _tampered(self, edit: Any) -> Any:
        rec = recorder()
        settled(rec)
        rec.execute(Post("p", sale(5)))
        doc = json.loads(dump_trace(rec.trace()))
        edit(doc)
        return replay_trace(parse_trace(doc))

    def test_edited_head_is_a_divergence(self) -> None:
        def edit(doc: Any) -> None:
            ok = [e for e in doc["events"] if e["type"] == "ledger_result" and e["ok"]]
            ok[-1]["head"] = "f" * 64

        report = self._tampered(edit)
        assert not report.consistent
        assert report.divergences[0].field_name == "head"
        assert "recorded 'fff" in str(report.divergences[0])

    def test_false_success_claim_is_a_divergence(self) -> None:
        def edit(doc: Any) -> None:
            cmds = [e for e in doc["events"] if e["type"] == "ledger_command"]
            cmds[1]["command"]["event"] = "settle"  # authorize -> settle without entry: fails

        report = self._tampered(edit)
        assert any(
            d.field_name == "ok" and d.recorded is True and d.recomputed is False
            for d in report.divergences
        )

    def test_false_replay_claim_is_a_divergence(self) -> None:
        def edit(doc: Any) -> None:
            # The authorize appended nothing, so claiming it was a replay is shape-valid
            # and only replay can catch the lie.
            auth = [
                e for e in doc["events"] if e["type"] == "ledger_result" and e["sequence"] == 0
            ][-1]
            auth["replayed"] = True

        report = self._tampered(edit)
        assert [d.field_name for d in report.divergences] == ["replayed"]

    def test_false_replay_claim_with_an_entry_is_rejected_at_parse(self) -> None:
        rec = recorder()
        rec.execute(Post("p", sale()))
        doc = json.loads(dump_trace(rec.trace()))
        doc["events"][-1]["replayed"] = True
        with pytest.raises(TraceError, match="appends nothing"):
            parse_trace(doc)

    def test_wrong_error_type_is_a_divergence(self) -> None:
        rec = recorder()
        rec.execute(OpenTransaction("o", "t", Money(1000, USD)))
        with pytest.raises(IllegalTransitionError):
            rec.execute(Refund("r", "t", Money(1, USD), sale(1)))
        doc = json.loads(dump_trace(rec.trace()))
        doc["events"][-1]["error"]["type"] = "SomethingElse"
        report = replay_trace(parse_trace(doc))
        assert any(d.field_name == "error.type" for d in report.divergences)

    def test_missing_result_is_rejected_at_parse(self) -> None:
        rec = recorder()
        rec.execute(Post("p", sale()))
        doc = json.loads(dump_trace(rec.trace()))
        doc["events"] = [e for e in doc["events"] if e["type"] != "ledger_result"]
        with pytest.raises(TraceError, match="without a result"):
            parse_trace(doc)

    def test_error_message_tamper_is_a_divergence(self) -> None:
        rec = recorder()
        rec.execute(OpenTransaction("o", "t", Money(1000, USD)))
        with pytest.raises(IllegalTransitionError):
            rec.execute(Refund("r", "t", Money(1, USD), sale(1)))
        doc = json.loads(dump_trace(rec.trace()))
        doc["events"][-1]["error"]["message"] = "accepted"
        report = replay_trace(parse_trace(doc))
        assert [d.field_name for d in report.divergences] == ["error.message"]

    def test_failed_result_head_and_sequence_are_compared(self) -> None:
        rec = recorder()
        rec.execute(Post("p", sale()))
        with pytest.raises(UnknownTransactionError):
            rec.execute(Refund("r", "t", Money(1, USD), sale(1)))
        doc = json.loads(dump_trace(rec.trace()))
        doc["events"][-1]["head"] = "f" * 64
        doc["events"][-1]["sequence"] = 9
        report = replay_trace(parse_trace(doc))
        assert {d.field_name for d in report.divergences} == {"head", "sequence"}

    def test_conversion_failure_is_the_ledgers_verdict_not_a_crash(self) -> None:
        """An external trace may carry an unbalanced or zero-amount entry; replay compares
        the ledger's rejection against what was recorded instead of raising."""
        rec = recorder()
        settled(rec)
        doc = json.loads(dump_trace(rec.trace()))
        settle_cmd = next(
            e
            for e in doc["events"]
            if e["type"] == "ledger_command"
            and e["command"]["kind"] == "advance"
            and e["command"].get("entry")
        )
        settle_cmd["command"]["entry"]["postings"][0]["money"]["amount"] = 1  # now unbalanced
        report = replay_trace(parse_trace(doc))
        first = report.divergences[0]
        assert first.field_name == "ok" and first.recorded is True and first.recomputed is False

    def test_undeclared_currency_is_rejected_before_replay(self) -> None:
        rec = recorder()
        rec.execute(Post("p", sale()))
        doc = json.loads(dump_trace(rec.trace()))
        doc["chart"].append({"account_id": "cad", "kind": "asset", "currency": "CAD"})
        with pytest.raises(TraceError, match="not declared"):
            parse_trace(doc)
        doc["currencies"].append({"code": "CAD", "exponent": 2})
        assert replay_trace(parse_trace(doc)).consistent

    def test_divergence_does_not_cascade_effects_into_next_command(self) -> None:
        rec = recorder()
        rec.execute(Post("a", sale(1)))
        rec.execute(Post("b", sale(2)))
        doc = json.loads(dump_trace(rec.trace()))
        results = [e for e in doc["events"] if e["type"] == "ledger_result"]
        results[0]["head"] = "f" * 64  # only the first is tampered
        report = replay_trace(parse_trace(doc))
        assert [d.command_id for d in report.divergences] == ["cmd-000001"]

    def test_dropped_effects_are_a_shape_violation(self) -> None:
        """A successful append without entry_id/posted_at is not a valid result any more."""
        rec = recorder()
        rec.execute(Post("p", sale()))
        doc = json.loads(dump_trace(rec.trace()))
        res = doc["events"][-1]
        del res["entry_id"], res["posted_at"]
        # Shape is still valid (both absent), but the head cannot be reproduced without them.
        report = replay_trace(parse_trace(doc))
        assert {d.field_name for d in report.divergences} >= {"head", "entry_id", "posted_at"}
