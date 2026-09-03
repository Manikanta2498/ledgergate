"""A trace recorded through a redactor carries no caller content and still replays exactly,
because every transform happens before the ledger hashes anything."""

from __future__ import annotations

import pytest

from ledgergate.codec import REDACTION_PATTERN, TOKEN_PATTERN, Tokenizer
from ledgergate.ledger import (
    EPOCH,
    USD,
    Account,
    AccountType,
    Advance,
    ChartOfAccounts,
    EntryDraft,
    Money,
    OpenTransaction,
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
    MessageEvent,
    Recorder,
    ToolCallEvent,
    ToolResultEvent,
    dump_trace,
    load_trace,
    replay_trace,
)

TK = Tokenizer(bytes(range(32)), domain="acme", key_version="v1")
CHART = ChartOfAccounts(
    [Account("cash", AccountType.ASSET, USD), Account("revenue", AccountType.REVENUE, USD)]
)
SECRETS = ("txn-alice", "alice@example.com", "SO-1", "order-42", "call-7", "card 4111")


def sale(n: int) -> EntryDraft:
    return EntryDraft.of(
        debit("cash", Money(n, USD)),
        credit("revenue", Money(n, USD)),
        description="alice@example.com",
        order="SO-1",
    )


def recorded() -> Recorder:
    rec = Recorder(
        "t", AgentDoc(name="a"), CHART, SteppingClock(EPOCH), SequentialIds(), redactor=TK
    )
    rec.message("user", "refund alice, card 4111")
    rec.tool_call(
        "call-7",
        "open_transaction",
        {"transaction_id": "txn-alice", "n": 1},
        idempotency_key="order-42",
    )
    rec.execute(OpenTransaction("order-42", "txn-alice", Money(1000, USD)), call_id="call-7")
    rec.tool_result("call-7", True, {"transaction": "txn-alice"})
    rec.execute(Advance("a", "txn-alice", TransactionEvent.AUTHORIZE))
    rec.execute(Advance("s", "txn-alice", TransactionEvent.SETTLE, sale(1000)))
    rec.tool_call("call-8", "refund", {"transaction_id": "txn-bob"}, idempotency_key="r")
    with pytest.raises(UnknownTransactionError):
        rec.execute(Refund("r", "txn-bob", Money(1, USD)), call_id="call-8")
    rec.tool_result("call-8", False, error=UnknownTransactionError("txn-bob"))
    return rec


class TestRedactedTrace:
    def test_no_raw_value_appears_anywhere_in_the_document(self) -> None:
        text = dump_trace(recorded().trace())
        for secret in (*SECRETS, "txn-bob"):
            assert secret not in text, secret
        assert '"cash"' in text and "1000" in text  # the books stay in the clear

    def test_every_field_is_transformed_by_its_class(self) -> None:
        events = recorded().trace().events
        msg = next(e for e in events if isinstance(e, MessageEvent))
        call = next(e for e in events if isinstance(e, ToolCallEvent))
        results = [e for e in events if isinstance(e, ToolResultEvent)]
        cmd = next(e for e in events if isinstance(e, LedgerCommandEvent))
        failed = next(e for e in events if isinstance(e, LedgerResultEvent) and not e.ok)
        assert REDACTION_PATTERN.match(msg.content)
        assert TOKEN_PATTERN.match(call.call_id) and TOKEN_PATTERN.match(call.idempotency_key or "")
        args = call.arguments
        assert set(args) == {TK.redact("transaction_id"), TK.redact("n")}  # keys redacted too
        assert REDACTION_PATTERN.match(str(args[TK.redact("transaction_id")]))
        assert args[TK.redact("n")] == TK.redact("1")  # numbers are redacted in untyped JSON
        first_result = results[0].result
        assert isinstance(first_result, dict)
        assert REDACTION_PATTERN.match(str(first_result[TK.redact("transaction")]))
        assert results[1].error is not None and REDACTION_PATTERN.match(results[1].error.message)
        assert cmd.command.key == TK.tokenize("order-42") and cmd.call_id == call.call_id
        # the core saw only tokens, so its message names a token and needs no redaction
        assert failed.error is not None and TK.tokenize("txn-bob") in failed.error.message

    def test_trace_header_fields_follow_their_classes(self) -> None:
        rec = Recorder(
            "run-alice",
            AgentDoc(name="a"),
            CHART,
            SteppingClock(EPOCH),
            SequentialIds(),
            scenario_id="refund-basic",
            metadata={"operator": "alice@example.com"},
            redactor=TK,
        )
        trace = rec.trace()
        assert trace.trace_id == TK.tokenize("run-alice") and trace.scenario_id == "refund-basic"
        (meta_key,) = trace.metadata
        assert REDACTION_PATTERN.match(meta_key) and REDACTION_PATTERN.match(
            trace.metadata[meta_key]
        )
        assert trace.chart is not None
        assert all(a.name == "" or REDACTION_PATTERN.match(a.name) for a in trace.chart)

    def test_a_redacted_trace_replays_exactly_with_no_key(self) -> None:
        rec = recorded()
        trace = load_trace(dump_trace(rec.trace()))  # through the published path
        report = replay_trace(trace)
        assert report.consistent, report.divergences
        assert report.ledger.head == rec.ledger.head

    def test_same_key_same_trace_different_key_different_trace(self) -> None:
        a = dump_trace(recorded().trace())
        b = dump_trace(recorded().trace())
        assert a == b
        other = Recorder(
            "t",
            AgentDoc(name="a"),
            CHART,
            SteppingClock(EPOCH),
            SequentialIds(),
            redactor=Tokenizer(bytes(32), domain="acme", key_version="v1"),
        )
        other.execute(OpenTransaction("order-42", "txn-alice", Money(1000, USD)))
        first = next(e for e in recorded().trace().events if isinstance(e, LedgerCommandEvent))
        assert other.trace().events[0].command.key != first.command.key  # type: ignore[union-attr]


class TestRecorderClosesTheSameGaps:
    def test_unresolved_entry_reference_is_refused_before_anything_is_recorded(self) -> None:
        rec = Recorder(
            "t", AgentDoc(name="a"), CHART, SteppingClock(EPOCH), SequentialIds(), redactor=TK
        )
        from ledgergate.ledger import Reverse, UnknownEntryError

        with pytest.raises(UnknownEntryError):
            rec.execute(Reverse("k", "jane.doe@example.com", "why"))
        assert rec.events == []
        assert "jane.doe" not in dump_trace(rec.trace())
        applied = rec.execute(OpenTransaction("o", "t", Money(1, USD)))
        assert applied.ledger.sequence == 0

    def test_tag_keys_are_redacted_and_the_trace_still_replays(self) -> None:
        rec = Recorder(
            "t", AgentDoc(name="a"), CHART, SteppingClock(EPOCH), SequentialIds(), redactor=TK
        )
        from ledgergate.ledger import Post

        draft = EntryDraft.of(
            debit("cash", Money(5, USD)),
            credit("revenue", Money(5, USD)),
            **{"card 4111111111111111": "x", "ssn": "123-45-6789"},
        )
        rec.execute(Post("k", draft))
        text = dump_trace(rec.trace())
        assert "4111" not in text and "123-45" not in text and '"ssn"' not in text
        assert replay_trace(load_trace(text)).consistent

    def test_invalid_trace_id_fails_at_construction(self) -> None:
        from ledgergate.ledger import InvalidIdentifierError

        with pytest.raises(InvalidIdentifierError):
            Recorder(
                "two\nlines",
                AgentDoc(name="a"),
                CHART,
                SteppingClock(EPOCH),
                SequentialIds(),
                redactor=TK,
            )


class TestRecorderResolvesReferencesLikeAdmission:
    def _rec(self, tools: frozenset[str] | None = None) -> Recorder:
        return Recorder(
            "t",
            AgentDoc(name="a"),
            CHART,
            SteppingClock(EPOCH),
            SequentialIds(),
            redactor=TK,
            tools=tools,
        )

    def test_unknown_account_is_refused_before_anything_is_recorded(self) -> None:
        from ledgergate.ledger import Post, UnknownAccountError

        rec = self._rec()
        draft = EntryDraft.of(
            debit("alice@example.com 4111111111111111", Money(5, USD)),
            credit("revenue", Money(5, USD)),
        )
        with pytest.raises(UnknownAccountError):
            rec.execute(Post("k", draft))
        assert rec.events == [] and "alice" not in dump_trace(rec.trace())

    def test_large_integers_in_tool_payloads_are_redacted_not_refused(self) -> None:
        rec = self._rec()
        rec.tool_call("c1", "lookup", {"n": 10**17, "m": 2**63 - 1, "f": 1.5})
        (call,) = rec.events
        assert isinstance(call, ToolCallEvent)
        assert set(call.arguments.values()) == {
            TK.redact(str(10**17)),
            TK.redact(str(2**63 - 1)),
            TK.redact("1.5"),
        }

    def test_undeclared_tool_names_are_redacted_declared_ones_kept(self) -> None:
        rec = self._rec(tools=frozenset({"lookup"}))
        rec.tool_call("c1", "lookup", {})
        rec.tool_call("c2", "transfer_everything_to_me", {})
        first, second = rec.events
        assert isinstance(first, ToolCallEvent) and isinstance(second, ToolCallEvent)
        assert first.tool == "lookup" and REDACTION_PATTERN.match(second.tool)
        bare = self._rec()
        bare.tool_call("c1", "lookup", {})
        (only,) = bare.events
        assert isinstance(only, ToolCallEvent) and REDACTION_PATTERN.match(only.tool)

    def test_metadata_keys_are_redacted_too(self) -> None:
        rec = Recorder(
            "t",
            AgentDoc(name="a"),
            CHART,
            SteppingClock(EPOCH),
            SequentialIds(),
            metadata={"operator email": "a@b.c"},
            redactor=TK,
        )
        (key,) = rec.trace().metadata
        assert REDACTION_PATTERN.match(key)
