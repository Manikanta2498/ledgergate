"""Trace models, conversions to and from the ledger core, and file I/O."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ledgergate.ledger import (
    EPOCH,
    EUR,
    USD,
    Account,
    AccountType,
    Advance,
    Command,
    EntryDraft,
    Money,
    OpenTransaction,
    Post,
    Refund,
    Reverse,
    TransactionEvent,
    UnbalancedEntryError,
    credit,
    debit,
)
from ledgergate.trace import (
    AccountDoc,
    AgentDoc,
    EntryDraftDoc,
    MoneyDoc,
    PositiveMoneyDoc,
    PostingDoc,
    SchemaNotFoundError,
    ToolCallEvent,
    Trace,
    TraceError,
    command_doc,
    default_schema_path,
    dump_trace,
    load_trace,
    parse_trace,
    write_trace,
)

E = TransactionEvent
AT = EPOCH


def minimal(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": "1",
        "trace_id": "t",
        "agent": {"name": "a"},
        "started_at": "2026-01-01T00:00:00Z",
        "events": [],
    }
    return {**base, **overrides}


class TestConversions:
    def test_money_round_trip(self) -> None:
        m = Money(-1999, USD)
        assert MoneyDoc.of(m).to_money() == m
        assert PositiveMoneyDoc.of(Money(1, EUR)).to_money() == Money(1, EUR)

    def test_positive_money_rejects_zero_and_negative(self) -> None:
        for amount in (0, -1):
            with pytest.raises(ValueError):
                PositiveMoneyDoc(amount=amount, currency="USD")

    def test_unknown_currency_fails_at_conversion_not_parse(self) -> None:
        doc = MoneyDoc(amount=1, currency="ZZZ")  # schema-valid shape
        with pytest.raises(Exception, match="unknown currency"):
            doc.to_money()

    def test_posting_and_draft_round_trip(self) -> None:
        draft = EntryDraft.of(
            debit("cash", Money(5, USD)),
            credit("revenue", Money(5, USD)),
            description="d",
            b="2",
            a="1",
        )
        doc = EntryDraftDoc.of(draft)
        assert doc.tags == {"a": "1", "b": "2"}
        assert doc.to_draft() == draft
        assert PostingDoc.of(draft.postings[0]).to_posting() == draft.postings[0]

    def test_unbalanced_draft_doc_fails_at_conversion(self) -> None:
        """The schema only requires two postings; balance is the ledger's check."""
        doc = EntryDraftDoc(
            postings=[
                PostingDoc(
                    account="cash", side="debit", money=PositiveMoneyDoc(amount=2, currency="USD")
                ),
                PostingDoc(
                    account="revenue",
                    side="credit",
                    money=PositiveMoneyDoc(amount=1, currency="USD"),
                ),
            ]
        )
        with pytest.raises(UnbalancedEntryError):
            doc.to_draft()

    def test_account_round_trip(self) -> None:
        acct = Account("w", AccountType.LIABILITY, USD, allow_negative=False, name="Wallet")
        assert AccountDoc.of(acct).to_account() == acct

    @pytest.mark.parametrize(
        "command",
        [
            Post(
                "k", EntryDraft.of(debit("cash", Money(1, USD)), credit("revenue", Money(1, USD)))
            ),
            Reverse("k", "e-1", "oops"),
            Reverse("k", "e-1"),
            OpenTransaction("k", "t", Money(100, USD)),
            Advance("k", "t", E.AUTHORIZE),
            Advance(
                "k",
                "t",
                E.SETTLE,
                EntryDraft.of(debit("cash", Money(1, USD)), credit("revenue", Money(1, USD))),
            ),
            Refund(
                "k",
                "t",
                Money(1, USD),
                EntryDraft.of(debit("revenue", Money(1, USD)), credit("cash", Money(1, USD))),
            ),
            Refund("k", "t", Money(1, USD)),
        ],
    )
    def test_every_command_round_trips(self, command: Command) -> None:
        assert command_doc(command).to_command() == command


class TestTraceValidation:
    def test_minimal_parses(self) -> None:
        trace = parse_trace(minimal())
        assert trace.trace_id == "t" and trace.events == [] and trace.metadata == {}

    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(TraceError) as exc:
            parse_trace(minimal(extra=1))
        assert exc.value.problems and "/extra" in exc.value.problems[0]

    def test_naive_timestamp_is_rejected(self) -> None:
        with pytest.raises(TraceError, match="timezone"):
            parse_trace(minimal(started_at="2026-01-01T00:00:00"))

    def test_ended_before_started_is_rejected(self) -> None:
        with pytest.raises(TraceError, match="precedes"):
            parse_trace(minimal(ended_at="2025-12-31T00:00:00Z"))

    def test_seq_must_strictly_increase(self) -> None:
        ev = {"type": "message", "at": "2026-01-01T00:00:00Z", "role": "user", "content": "x"}
        with pytest.raises(TraceError, match="strictly increasing"):
            parse_trace(minimal(events=[{**ev, "seq": 2}, {**ev, "seq": 2}]))
        with pytest.raises(TraceError, match="strictly increasing"):
            parse_trace(minimal(events=[{**ev, "seq": 2}, {**ev, "seq": 1}]))
        assert len(parse_trace(minimal(events=[{**ev, "seq": 1}, {**ev, "seq": 7}])).events) == 2

    def test_whole_float_is_refused_for_integers(self) -> None:
        ev = {
            "type": "message",
            "at": "2026-01-01T00:00:00Z",
            "role": "user",
            "content": "x",
            "seq": 1.0,
        }
        with pytest.raises(TraceError):
            parse_trace(minimal(events=[ev]))

    def test_error_lists_every_problem(self) -> None:
        with pytest.raises(TraceError) as exc:
            parse_trace(minimal(trace_id="", agent={"name": ""}, started_at="bad"))
        assert len(exc.value.problems) >= 3
        assert "+2 more" in str(exc.value) or "more" in str(exc.value)

    def test_chart_of_accounts_requires_chart(self) -> None:
        with pytest.raises(ValueError, match="no chart"):
            parse_trace(minimal()).chart_of_accounts()
        trace = parse_trace(
            minimal(chart=[{"account_id": "cash", "kind": "asset", "currency": "USD"}])
        )
        assert trace.chart_of_accounts()["cash"].kind is AccountType.ASSET

    def test_tool_call_event_arguments_are_free_form(self) -> None:
        ev = ToolCallEvent(
            seq=1, at=AT, call_id="c", tool="refund", arguments={"nested": {"x": [1, 2]}}
        )
        assert ev.arguments["nested"] == {"x": [1, 2]}
        assert ev.idempotency_key is None


class TestIO:
    def test_dump_is_canonical_and_deterministic(self) -> None:
        trace = parse_trace(minimal(metadata={"z": "1", "a": "2"}))
        text = dump_trace(trace)
        assert text == dump_trace(trace)
        assert text.endswith("\n")
        assert text.index('"a": "2"') < text.index('"z": "1"'), "keys are sorted"
        assert "null" not in text, "None fields are omitted, not serialized"

    def test_load_accepts_str_bytes_and_path(self, tmp_path: Path) -> None:
        trace = parse_trace(minimal())
        text = dump_trace(trace)
        assert load_trace(text) == trace
        assert load_trace(text.encode()) == trace
        path = tmp_path / "t.json"
        write_trace(trace, path)
        assert load_trace(path) == trace

    def test_load_reports_problems(self) -> None:
        with pytest.raises(TraceError) as exc:
            load_trace(json.dumps(minimal(trace_id=" x")))
        assert exc.value.problems[0].startswith("/trace_id")

    def test_default_schema_path_finds_repo_schema(self) -> None:
        path = default_schema_path()
        assert path.name == "v1.json" and path.parent.name == "trace"

    def test_default_schema_path_fails_closed_outside_a_checkout(self, tmp_path: Path) -> None:
        with pytest.raises(SchemaNotFoundError, match="pass its path explicitly"):
            default_schema_path(tmp_path / "nowhere" / "x.py")

    def test_timestamps_survive_round_trip_with_offset(self) -> None:
        at = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC) + timedelta(microseconds=123456)
        trace = Trace(trace_id="t", agent=AgentDoc(name="a"), started_at=at, events=[])
        assert load_trace(dump_trace(trace)).started_at == at
