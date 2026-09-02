"""Trace models, conversions to and from the ledger core, and file I/O."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ledgergate.ledger import (
    CURRENCIES,
    EPOCH,
    EUR,
    USD,
    Account,
    AccountType,
    Advance,
    Command,
    EntryDraft,
    InvalidAmountError,
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
    CurrencyDoc,
    EntryDraftDoc,
    LedgerResultEvent,
    MoneyDoc,
    PostingDoc,
    SchemaNotFoundError,
    ToolCallEvent,
    Trace,
    TraceError,
    command_currencies,
    command_doc,
    default_schema_path,
    dump_trace,
    load_trace,
    parse_trace,
    write_trace,
)

E = TransactionEvent
AT = EPOCH
REG = CURRENCIES


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
        assert MoneyDoc.of(m).to_money(REG) == m

    def test_money_is_not_constrained_positive_at_parse(self) -> None:
        """A trace records attempts; the ledger rejects them at replay."""
        assert MoneyDoc(amount=0, currency="USD").to_money(REG) == Money(0, USD)
        assert MoneyDoc(amount=-5, currency="USD").amount == -5

    def test_currency_resolution_uses_the_registry(self) -> None:
        doc = MoneyDoc(amount=1500, currency="CAD")
        with pytest.raises(LookupError, match="not declared"):
            doc.to_money(REG)
        cad = CurrencyDoc(code="CAD", exponent=2).to_currency()
        assert doc.to_money({**REG, "CAD": cad}) == Money(1500, cad)

    def test_currency_doc_round_trip(self) -> None:
        for cur in CURRENCIES.values():
            assert CurrencyDoc.of(cur).to_currency() == cur

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
        assert doc.to_draft(REG) == draft
        assert PostingDoc.of(draft.postings[0]).to_posting(REG) == draft.postings[0]

    def test_unbalanced_and_nonpositive_drafts_fail_at_conversion_with_ledger_errors(self) -> None:
        """The schema only requires two postings; balance and sign are the ledger's checks."""
        two = EntryDraftDoc(
            postings=[
                PostingDoc(account="cash", side="debit", money=MoneyDoc(amount=2, currency="USD")),
                PostingDoc(
                    account="revenue", side="credit", money=MoneyDoc(amount=1, currency="USD")
                ),
            ]
        )
        with pytest.raises(UnbalancedEntryError):
            two.to_draft(REG)
        zero = EntryDraftDoc(
            postings=[
                PostingDoc(account="cash", side="debit", money=MoneyDoc(amount=0, currency="USD")),
                PostingDoc(
                    account="revenue", side="credit", money=MoneyDoc(amount=0, currency="USD")
                ),
            ]
        )
        with pytest.raises(InvalidAmountError):
            zero.to_draft(REG)

    def test_account_round_trip(self) -> None:
        acct = Account("w", AccountType.LIABILITY, USD, allow_negative=False, name="Wallet")
        assert AccountDoc.of(acct).to_account(REG) == acct

    @pytest.mark.parametrize(
        "command",
        [
            Post(
                "k", EntryDraft.of(debit("cash", Money(1, USD)), credit("revenue", Money(1, USD)))
            ),
            Reverse("k", "e-1", "oops"),
            Reverse("k", "e-1"),
            OpenTransaction("k", "t", Money(100, USD)),
            OpenTransaction("k", "t", Money(0, USD)),  # an invalid attempt is representable
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
            Refund("k", "t", Money(-1, USD)),
        ],
    )
    def test_every_command_round_trips(self, command: Command) -> None:
        assert command_doc(command).to_command(REG) == command

    def test_command_currencies_collects_every_currency_object(self) -> None:
        eur_draft = EntryDraft.of(debit("cash:eur", Money(1, EUR)), credit("fx:eur", Money(1, EUR)))
        assert command_currencies(Refund("k", "t", Money(1, USD), eur_draft)) == {USD, EUR}
        assert command_currencies(Reverse("k", "e")) == set()


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

    def test_result_shape_success(self) -> None:
        base = {
            "seq": 1,
            "at": "2026-01-01T00:00:00Z",
            "type": "ledger_result",
            "command_id": "c",
            "ok": True,
        }
        full = {
            **base,
            "replayed": False,
            "head": "0" * 64,
            "sequence": 1,
            "entry_id": "e",
            "posted_at": "2026-01-01T00:00:00Z",
        }
        assert LedgerResultEvent.model_validate(full).entry_id == "e"
        for missing in ("replayed", "head", "sequence"):
            with pytest.raises(ValueError):
                LedgerResultEvent.model_validate({k: v for k, v in full.items() if k != missing})
        with pytest.raises(ValueError, match="together"):
            LedgerResultEvent.model_validate({k: v for k, v in full.items() if k != "posted_at"})
        with pytest.raises(ValueError, match="must not carry an error"):
            LedgerResultEvent.model_validate({**full, "error": {"type": "X", "message": ""}})
        with pytest.raises(ValueError, match="replayed command appends nothing"):
            LedgerResultEvent.model_validate({**full, "replayed": True})

    def test_result_shape_failure(self) -> None:
        base = {
            "seq": 1,
            "at": "2026-01-01T00:00:00Z",
            "type": "ledger_result",
            "command_id": "c",
            "ok": False,
        }
        full = {**base, "error": {"type": "E", "message": "m"}, "head": "0" * 64, "sequence": 0}
        assert LedgerResultEvent.model_validate(full).error is not None
        with pytest.raises(ValueError, match="requires error"):
            LedgerResultEvent.model_validate({k: v for k, v in full.items() if k != "error"})
        for extra in ({"replayed": False}, {"entry_id": "e", "posted_at": "2026-01-01T00:00:00Z"}):
            with pytest.raises(ValueError, match="must not carry"):
                LedgerResultEvent.model_validate({**full, **extra})

    def test_naive_posted_at_is_rejected_like_every_other_timestamp(self) -> None:
        doc = {
            "seq": 1,
            "at": "2026-01-01T00:00:00Z",
            "type": "ledger_result",
            "command_id": "c",
            "ok": True,
            "replayed": False,
            "head": "0" * 64,
            "sequence": 1,
            "entry_id": "e",
            "posted_at": "2026-01-01T00:00:00",
        }
        with pytest.raises(ValueError, match="timezone"):
            LedgerResultEvent.model_validate(doc)

    def test_timestamps_are_normalized_to_utc(self) -> None:
        trace = parse_trace(minimal(started_at="2025-12-31T19:00:00-05:00"))
        assert trace.started_at == datetime(2026, 1, 1, tzinfo=UTC)
        assert trace.started_at.tzinfo is UTC

    def _cmd(self, seq: int, cid: str) -> dict[str, object]:
        return {
            "seq": seq,
            "at": "2026-01-01T00:00:00Z",
            "type": "ledger_command",
            "command_id": cid,
            "command": {"kind": "reverse", "key": "k", "entry_id": "e"},
        }

    def _res(self, seq: int, cid: str) -> dict[str, object]:
        return {
            "seq": seq,
            "at": "2026-01-01T00:00:00Z",
            "type": "ledger_result",
            "command_id": cid,
            "ok": False,
            "error": {"type": "E", "message": ""},
            "head": "0" * 64,
            "sequence": 0,
        }

    def test_command_result_pairing(self) -> None:
        good = minimal(events=[self._cmd(1, "a"), self._res(2, "a")])
        assert len(parse_trace(good).results()) == 1
        cases = {
            "must be unique": [self._cmd(1, "a"), self._cmd(2, "a"), self._res(3, "a")],
            "only one ledger_result": [self._cmd(1, "a"), self._res(2, "a"), self._res(3, "a")],
            "without a command": [self._cmd(1, "a"), self._res(2, "a"), self._res(3, "ghost")],
            "without a result": [self._cmd(1, "a")],
            "precedes its command": [self._res(1, "a"), self._cmd(2, "a")],
        }
        for message, events in cases.items():
            with pytest.raises(TraceError, match=message):
                parse_trace(minimal(events=events))

    def test_currencies_must_resolve_and_not_contradict(self) -> None:
        cad_chart = [{"account_id": "c", "kind": "asset", "currency": "CAD"}]
        with pytest.raises(TraceError, match="not declared and not bundled"):
            parse_trace(minimal(chart=cad_chart))
        trace = parse_trace(minimal(chart=cad_chart, currencies=[{"code": "CAD", "exponent": 2}]))
        assert trace.chart_of_accounts()["c"].currency.exponent == 2
        with pytest.raises(TraceError, match="bundled exponent"):
            parse_trace(minimal(currencies=[{"code": "USD", "exponent": 3}]))
        with pytest.raises(TraceError, match="more than once"):
            parse_trace(
                minimal(currencies=[{"code": "CAD", "exponent": 2}, {"code": "CAD", "exponent": 2}])
            )

    @pytest.mark.parametrize(
        "sep", ["\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029", "\x00"]
    )
    def test_identifiers_reject_every_line_break(self, sep: str) -> None:
        with pytest.raises(TraceError):
            parse_trace(minimal(trace_id=f"a{sep}b"))

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
