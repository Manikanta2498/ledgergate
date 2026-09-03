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
        assert REDACTION_PATTERN.match(str(call.arguments["transaction_id"]))
        assert call.arguments["n"] == 1
        first_result = results[0].result
        assert isinstance(first_result, dict)
        assert REDACTION_PATTERN.match(str(first_result["transaction"]))
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
        assert REDACTION_PATTERN.match(trace.metadata["operator"])
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
