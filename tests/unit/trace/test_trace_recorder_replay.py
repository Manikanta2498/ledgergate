"""Recording a session into a trace, and replaying a trace against the core."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ledgergate.ledger import (
    EPOCH,
    USD,
    Account,
    AccountType,
    Advance,
    ChartOfAccounts,
    EntryDraft,
    IllegalTransitionError,
    Money,
    OpenTransaction,
    Post,
    Refund,
    SequentialIds,
    SteppingClock,
    TransactionEvent,
    credit,
    debit,
)
from ledgergate.trace import (
    AgentDoc,
    LedgerCommandEvent,
    LedgerResultEvent,
    Recorder,
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
            ok = [e for e in doc["events"] if e["type"] == "ledger_result" and e["ok"]]
            ok[-1]["replayed"] = True

        report = self._tampered(edit)
        assert any(d.field_name == "replayed" for d in report.divergences)

    def test_wrong_error_type_is_a_divergence(self) -> None:
        rec = recorder()
        rec.execute(OpenTransaction("o", "t", Money(1000, USD)))
        with pytest.raises(IllegalTransitionError):
            rec.execute(Refund("r", "t", Money(1, USD), sale(1)))
        doc = json.loads(dump_trace(rec.trace()))
        doc["events"][-1]["error"]["type"] = "SomethingElse"
        report = replay_trace(parse_trace(doc))
        assert any(d.field_name == "error.type" for d in report.divergences)

    def test_missing_result_is_reported(self) -> None:
        rec = recorder()
        rec.execute(Post("p", sale()))
        doc = json.loads(dump_trace(rec.trace()))
        doc["events"] = [e for e in doc["events"] if e["type"] != "ledger_result"]
        report = replay_trace(parse_trace(doc))
        assert report.missing_results == ("cmd-000001",) and not report.consistent

    def test_dropped_effects_surface_as_head_mismatch(self) -> None:
        """A trace that omits entry_id/posted_at cannot reproduce its own heads."""
        rec = recorder()
        rec.execute(Post("p", sale()))
        doc = json.loads(dump_trace(rec.trace()))
        res = doc["events"][-1]
        del res["entry_id"], res["posted_at"]
        report = replay_trace(parse_trace(doc))
        assert any(d.field_name in ("head", "entry_id") for d in report.divergences)
