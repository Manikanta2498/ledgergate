"""Trace v2 review findings: what a read's digest covers, how many pairs may replay, the
lossless legacy digest, and the two v1 documents that recorded, satisfied the v1 schema and
then broke the v2 read path.

Each test is the negative regression for one finding: it forges or constructs a document that
previously passed, or previously raised out of `load_any`/`verify`, and pins the answer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ledgergate.derive import trace as derive
from ledgergate.invariants import check
from ledgergate.journal import Journal
from ledgergate.ledger import (
    EPOCH,
    USD,
    Account,
    AccountType,
    ChartOfAccounts,
    EntryDraft,
    Ledger,
    LedgerError,
    Money,
    Post,
    Posting,
    SequentialIds,
    Side,
    SteppingClock,
)
from ledgergate.trace import (
    AgentDoc,
    MessageEvent,
    Recorder,
    TraceError,
    TraceV2,
    dump_trace,
    load_any,
    load_trace,
    replay_trace,
)
from ledgergate.trace.io import validate_document
from ledgergate.trace.models import (
    AccountDoc,
    CurrencyDoc,
    ErrorDoc,
    LedgerCommandEvent,
    LedgerResultEvent,
    ReverseDoc,
)
from ledgergate.trace.v2 import (
    InvocationResolution,
    LegacyIntent,
    legacy_command_digest,
    read_request_digest,
)

CHART = ChartOfAccounts(
    [Account("cash", AccountType.ASSET, USD), Account("revenue", AccountType.REVENUE, USD)]
)


def _sale(amount: int, tags: tuple[tuple[str, str], ...] = ()) -> EntryDraft:
    return EntryDraft(
        (
            Posting("cash", Side.DEBIT, Money(amount, USD)),
            Posting("revenue", Side.CREDIT, Money(amount, USD)),
        ),
        "",
        tags,
    )


class TestAReadIsBoundToItsPrincipal:
    """A read's `attempted_digest` *is* its `request_digest`, and that hashes the principal
    (journal/admission.py, `Request.request_digest`). Reassigning the read to another live
    principal was a PASS: every liveness check the forged row had was satisfied."""

    def _journal(self, tmp_path: Path) -> str:
        path = str(tmp_path / "r.journal")
        j = Journal.create(path, CHART, clock=SteppingClock(EPOCH), ids=SequentialIds())
        j.add_principal("ops", "transport", None)
        j.handle({"tool": "balance", "call_id": "c1", "arguments": {"account": "cash"}})
        j.close()
        return path

    def test_a_read_reassigned_to_another_live_principal_is_refused_at_load(
        self, tmp_path: Path
    ) -> None:
        t = derive(self._journal(tmp_path))
        assert check(t).passed
        doc = t.model_dump(mode="json", exclude_none=True)
        for e in doc["events"]:
            if e["type"] == "invocation_resolution":
                e["principal"] = "ops"
            elif e["type"] == "policy_decision":
                e["context"]["principal"] = "ops"
        with pytest.raises(ValidationError, match="not the read's request digest"):
            TraceV2.model_validate(doc)

    def test_the_recomputation_is_the_journals_own(self, tmp_path: Path) -> None:
        t = derive(self._journal(tmp_path))
        read = next(e for e in t.events if e.type == "read_intent")
        resolution = next(r for r in t.resolutions() if r.disposition == "read")
        assert resolution.principal is not None
        assert read_request_digest(read, resolution.principal) == resolution.attempted_digest


class TestReplayIsNotBoundedByTheV1DocumentLimit:
    """`ledger_view()` used to build a *validated* v1 document, so a v2 trace with 50,001
    ledger pairs (100,002 v1 events) could not be verified at all, though the journal was
    well within its own capacity."""

    def _document(self, pairs: int) -> TraceV2:
        ledger = Ledger.empty(CHART)
        try:
            ledger.execute(
                ReverseDoc(key="k", entry_id="ghost").to_command({"USD": USD}),
                clock=SteppingClock(EPOCH),
                ids=SequentialIds(),
            )
        except LedgerError as exc:
            error = ErrorDoc(type=type(exc).__name__, message=str(exc))
        events: list[Any] = []
        for i in range(pairs):
            command = ReverseDoc(key=f"k{i}", entry_id="ghost")
            intent, operation = f"legacy-{i + 1}", f"command{i}"
            events.append(LegacyIntent(seq=1, at=EPOCH, intent_id=intent, command=command))
            events.append(
                InvocationResolution(
                    seq=1,
                    at=EPOCH,
                    intent_id=intent,
                    disposition="legacy",
                    operation_id=operation,
                    attempted_digest=legacy_command_digest(command),
                )
            )
            events.append(
                LedgerCommandEvent(seq=1, at=EPOCH, command_id=operation, command=command)
            )
            events.append(
                LedgerResultEvent(
                    seq=1,
                    at=EPOCH,
                    command_id=operation,
                    ok=False,
                    error=error,
                    head=ledger.head,
                    sequence=ledger.sequence,
                )
            )
        return TraceV2(
            trace_id="t",
            started_at=EPOCH,
            ended_at=EPOCH,
            policy_set_version="legacy",
            currencies=(CurrencyDoc.of(USD),),
            chart=tuple(AccountDoc.of(a) for a in CHART.values()),
            events=tuple(e.model_copy(update={"seq": i + 1}) for i, e in enumerate(events)),
        )

    def test_fifty_thousand_and_one_pairs_replay(self) -> None:
        t = self._document(50_001)
        report = replay_trace(t.ledger_view())
        assert report.commands_replayed == 50_001
        assert report.consistent

    def test_the_view_still_carries_only_the_pairs_and_the_chart(self) -> None:
        view = self._document(2).ledger_view()
        assert [e.type for e in view.events] == [
            "ledger_command",
            "ledger_result",
            "ledger_command",
            "ledger_result",
        ]
        assert view.commands()[0].call_id is None
        assert sorted(view.chart_of_accounts()) == ["cash", "revenue"]

    def test_a_currency_no_pair_can_resolve_is_refused_at_load(self) -> None:
        doc = self._document(1).model_dump(mode="json", exclude_none=True)
        doc["currencies"] = []
        doc["chart"] = [{"account_id": "cash", "kind": "asset", "currency": "XTS"}]
        with pytest.raises(ValidationError, match="not declared and not bundled"):
            TraceV2.model_validate(doc)


class TestLegacyDocumentsThatV1AdmitsAndV2MustRead:
    def _recorded(self, draft: EntryDraft) -> str:
        rec = Recorder("t", AgentDoc(name="a"), CHART, SteppingClock(EPOCH), SequentialIds())
        rec.execute(Post("k", draft))
        return dump_trace(rec.trace())

    def test_an_amount_outside_the_ijson_range_lifts_and_verifies(self) -> None:
        """v1 bounds only payloads to the safe range; a `Money.amount` is an unbounded
        integer there, and JCS has no serialization for one, so `load_any` raised
        `JcsError` out of the lift."""
        text = self._recorded(_sale(2**53))
        validate_document(json.loads(text))
        assert replay_trace(load_trace(text)).consistent
        t = load_any(text)
        assert check(t).passed
        resolution = next(r for r in t.resolutions() if r.disposition == "legacy")
        intent = next(e for e in t.events if e.type == "legacy_intent")
        assert legacy_command_digest(intent.command) == resolution.attempted_digest

    def test_the_legacy_digest_still_separates_two_amounts_outside_the_range(self) -> None:
        digests = {
            r.attempted_digest
            for amount in (2**53, 2**53 + 1)
            for r in load_any(self._recorded(_sale(amount))).resolutions()
        }
        assert len(digests) == 2

    def test_a_tag_key_over_the_codecs_bound_is_a_replay_finding_not_an_exception(self) -> None:
        """v1's schema bounds the tag *count*, not a key's length; the codec bounds the
        length at decode, so `verify` raised `CodecError` out of the invariant registry."""
        text = self._recorded(_sale(5, tags=(("k" * 1025, "v"),)))
        validate_document(json.loads(text))
        t = load_any(text)  # the lifted digest is over the document, not over a decode
        card = check(t)
        assert not card.passed
        statuses = {r.name: r.status for r in card.results}
        assert statuses["ledger_pairs_replay"] == "fail"
        assert any("CodecError" in f.message for f in card.failures)


class TestCalendarEdgeTimestamps:
    @pytest.mark.parametrize(
        "stamp",
        [
            "0001-01-01T00:00:00+01:00",
            "0001-01-01T00:00:00.000001+14:00",
            "9999-12-31T23:59:59-12:00",
        ],
    )
    def test_a_stamp_with_no_utc_normalization_is_a_validation_error(self, stamp: str) -> None:
        """`_to_utc` raised `OverflowError`, which is not a `ValueError`, so it escaped
        pydantic and every caller of `load_trace` and `load_any`."""
        with pytest.raises(ValidationError, match="no UTC normalization"):
            MessageEvent(seq=1, at=stamp, role="user", content="x")

    def test_the_same_stamp_in_a_document_is_a_trace_error(self) -> None:
        rec = Recorder("t", AgentDoc(name="a"), CHART, SteppingClock(EPOCH), SequentialIds())
        doc = json.loads(dump_trace(rec.trace()))
        doc["started_at"] = "0001-01-01T00:00:00+01:00"
        with pytest.raises(TraceError, match="no UTC normalization"):
            load_any(json.dumps(doc))
