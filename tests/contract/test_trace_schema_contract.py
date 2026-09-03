"""The published JSON Schema and the runtime models must accept and reject the same documents.

A consumer on another stack sees only ``schema/trace/v1.json``. If the models were
stricter, a schema-valid trace would fail here; if looser, a trace this runtime emits
could fail a third-party validator. Either way the contract would be a fiction. These
tests hold the two to each other on both valid and invalid documents.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ledgergate.ledger import (
    EPOCH,
    USD,
    Account,
    AccountType,
    Advance,
    ChartOfAccounts,
    Command,
    EntryDraft,
    Money,
    OpenTransaction,
    Post,
    Refund,
    Reverse,
    SequentialIds,
    SteppingClock,
    TransactionEvent,
    credit,
    debit,
)
from ledgergate.trace import (
    AgentDoc,
    Recorder,
    TraceError,
    dump_trace,
    iter_schema_problems,
    load_schema,
    parse_trace,
    replay_trace,
    validate_document,
)

CHART = ChartOfAccounts(
    [
        Account("cash", AccountType.ASSET, USD),
        Account("revenue", AccountType.REVENUE, USD),
        Account("fees", AccountType.EXPENSE, USD),
    ]
)
E = TransactionEvent


def sale(amount: int) -> EntryDraft:
    return EntryDraft.of(debit("cash", Money(amount, USD)), credit("revenue", Money(amount, USD)))


def refund_entry(amount: int) -> EntryDraft:
    return EntryDraft.of(debit("revenue", Money(amount, USD)), credit("cash", Money(amount, USD)))


def recorded(commands: list[Command]) -> dict[str, Any]:
    """A trace document produced by the runtime for ``commands`` (errors included)."""
    rec = Recorder("t-1", AgentDoc(name="agent"), CHART, SteppingClock(EPOCH), SequentialIds())
    rec.message("user", "refund order 42")
    rec.tool_call("c1", "refund", {"order": "42"}, idempotency_key="k-42")
    rec.run(commands)
    rec.tool_result("c1", True, {"status": "done"})
    document: dict[str, Any] = json.loads(dump_trace(rec.trace()))
    return document


REFUND_FLOW: list[Command] = [
    OpenTransaction("open", "t", Money(1999, USD)),
    Advance("auth", "t", E.AUTHORIZE),
    Advance("settle", "t", E.SETTLE, sale(1999)),
    Refund("refund", "t", Money(500, USD), refund_entry(500)),
    Refund("refund", "t", Money(500, USD), refund_entry(500)),  # retry
    Post("fee", EntryDraft.of(debit("fees", Money(30, USD)), credit("cash", Money(30, USD)))),
    Reverse("undo-fee", "e-000003"),
    Refund("too-much", "t", Money(5000, USD), refund_entry(5000)),  # fails, recorded
]


def both_verdicts(document: Any) -> tuple[bool, bool]:
    """(schema accepts, models accept)."""
    schema_ok = not list(iter_schema_problems(document))
    try:
        parse_trace(document)
        models_ok = True
    except TraceError:
        models_ok = False
    return schema_ok, models_ok


class TestValidDocuments:
    def test_runtime_output_satisfies_the_published_schema(self) -> None:
        document = recorded(REFUND_FLOW)
        validate_document(document)
        trace = parse_trace(document)
        assert len(trace.commands()) == len(REFUND_FLOW)
        assert replay_trace(trace).consistent

    def test_minimal_document(self) -> None:
        minimal = {
            "schema_version": "1",
            "trace_id": "t",
            "agent": {"name": "a"},
            "started_at": "2026-01-01T00:00:00Z",
            "events": [],
        }
        assert both_verdicts(minimal) == (True, True)


def mutate(document: dict[str, Any], path: list[str | int], value: Any) -> dict[str, Any]:
    """Deep-copy ``document`` and set ``path`` to ``value`` (a sentinel removes the key)."""
    out: dict[str, Any] = json.loads(json.dumps(document))
    node: Any = out
    for step in path[:-1]:
        node = node[step]
    if value is REMOVE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return out


REMOVE = object()

BASE = recorded(REFUND_FLOW)
LEDGER_CMD = next(i for i, e in enumerate(BASE["events"]) if e["type"] == "ledger_command")
REFUND_CMD = next(
    i
    for i, e in enumerate(BASE["events"])
    if e["type"] == "ledger_command" and e["command"]["kind"] == "refund"
)
LEDGER_RES = next(i for i, e in enumerate(BASE["events"]) if e["type"] == "ledger_result")
TOOL_CALL = next(i for i, e in enumerate(BASE["events"]) if e["type"] == "tool_call")
TOOL_RES = next(i for i, e in enumerate(BASE["events"]) if e["type"] == "tool_result")
APPEND_RES = next(
    i for i, e in enumerate(BASE["events"]) if e["type"] == "ledger_result" and e.get("entry_id")
)
FAIL_RES = next(
    i for i, e in enumerate(BASE["events"]) if e["type"] == "ledger_result" and not e["ok"]
)

INVALID: dict[str, dict[str, Any]] = {
    "wrong schema version": mutate(BASE, ["schema_version"], "2"),
    "missing trace_id": mutate(BASE, ["trace_id"], REMOVE),
    "padded trace_id": mutate(BASE, ["trace_id"], " t-1"),
    "unknown top-level field": mutate(BASE, ["surprise"], 1),
    "naive started_at": mutate(BASE, ["started_at"], "2026-01-01T00:00:00"),
    "garbage started_at": mutate(BASE, ["started_at"], "yesterday"),
    "string money": mutate(BASE, ["events", REFUND_CMD, "command", "money", "amount"], "500"),
    "lowercase currency": mutate(
        BASE, ["events", REFUND_CMD, "command", "money", "currency"], "usd"
    ),
    "unknown event type": mutate(BASE, ["events", 0, "type"], "thought"),
    "event extra field": mutate(BASE, ["events", 0, "surprise"], True),
    "zero seq": mutate(BASE, ["events", 0, "seq"], 0),
    "refund via advance": mutate(
        BASE,
        ["events", LEDGER_CMD, "command"],
        {"kind": "advance", "key": "k", "transaction_id": "t", "event": "refund"},
    ),
    "unknown command kind": mutate(BASE, ["events", LEDGER_CMD, "command", "kind"], "teleport"),
    "command missing key": mutate(BASE, ["events", LEDGER_CMD, "command", "key"], REMOVE),
    "one-posting draft": mutate(
        BASE,
        ["events", REFUND_CMD, "command", "entry", "postings"],
        [BASE["events"][REFUND_CMD]["command"]["entry"]["postings"][0]],
    ),
    "bad side": mutate(
        BASE, ["events", REFUND_CMD, "command", "entry", "postings", 0, "side"], "left"
    ),
    "short head": mutate(BASE, ["events", LEDGER_RES, "head"], "abc"),
    "string ok": mutate(BASE, ["events", LEDGER_RES, "ok"], "true"),
    "bad account kind": mutate(BASE, ["chart", 0, "kind"], "cash"),
    "non-string metadata": mutate(BASE, ["metadata"], {"k": 1}),
    # Tool result shape
    "failed tool result without error": mutate(BASE, ["events", TOOL_RES, "ok"], False),
    "successful tool result with error": mutate(
        BASE, ["events", TOOL_RES, "error"], {"type": "E", "message": ""}
    ),
    # Result shape: success
    "success without head": mutate(BASE, ["events", LEDGER_RES, "head"], REMOVE),
    "success without sequence": mutate(BASE, ["events", LEDGER_RES, "sequence"], REMOVE),
    "success without replayed": mutate(BASE, ["events", LEDGER_RES, "replayed"], REMOVE),
    "success with error": mutate(
        BASE, ["events", LEDGER_RES, "error"], {"type": "X", "message": ""}
    ),
    "entry_id without posted_at": mutate(BASE, ["events", APPEND_RES, "posted_at"], REMOVE),
    "posted_at without entry_id": mutate(BASE, ["events", APPEND_RES, "entry_id"], REMOVE),
    "naive posted_at": mutate(BASE, ["events", APPEND_RES, "posted_at"], "2026-01-01T00:00:00"),
    # Result shape: failure
    "failure without error": mutate(BASE, ["events", FAIL_RES, "error"], REMOVE),
    "failure without head": mutate(BASE, ["events", FAIL_RES, "head"], REMOVE),
    "failure with replayed": mutate(BASE, ["events", FAIL_RES, "replayed"], False),
    "failure with entry effects": mutate(
        mutate(BASE, ["events", FAIL_RES, "entry_id"], "e"),
        ["events", FAIL_RES, "posted_at"],
        "2026-01-01T00:00:00Z",
    ),
    # Identifiers: every line separator, not only LF
    "CR in id": mutate(BASE, ["trace_id"], "a\rb"),
    "NEL in id": mutate(BASE, ["trace_id"], "a\x85b"),
    "LS in id": mutate(BASE, ["trace_id"], "a\u2028b"),
    "NUL in id": mutate(BASE, ["trace_id"], "a\x00b"),
    # Currencies
    "bad currency exponent": mutate(BASE, ["currencies", 0, "exponent"], 7),
    "currency def extra field": mutate(BASE, ["currencies", 0, "note"], "x"),
    # Limits
    "oversize message": mutate(BASE, ["events", 0, "content"], "x" * 65537),
    "oversize description": mutate(
        BASE, ["events", REFUND_CMD, "command", "entry", "description"], "x" * 1025
    ),
    "too many tags": mutate(
        BASE, ["events", REFUND_CMD, "command", "entry", "tags"], {f"k{i}": "v" for i in range(101)}
    ),
}


@pytest.mark.parametrize("name", sorted(INVALID))
def test_schema_and_models_reject_the_same_invalid_documents(name: str) -> None:
    schema_ok, models_ok = both_verdicts(INVALID[name])
    assert (schema_ok, models_ok) == (False, False), (name, schema_ok, models_ok)


@pytest.mark.parametrize(
    "path",
    [["events", REFUND_CMD, "command", "money", "amount"], ["events", 0, "seq"]],
    ids=["money", "seq"],
)
def test_runtime_is_stricter_than_json_can_express_about_floats(path: list[str | int]) -> None:
    """``5.0`` passes ``"type": "integer"``: the JSON data model has one number type and the
    spec defines integer as zero fractional part. The runtime refuses it anyway, because
    a float that happens to be whole is still a float, and the next one may not be. This
    is a deliberate, pinned asymmetry: the runtime is stricter than the schema *can* be."""
    whole_float = mutate(BASE, path, 5.0)
    schema_ok, models_ok = both_verdicts(whole_float)
    assert schema_ok is True and models_ok is False


RUNTIME_ONLY: dict[str, dict[str, Any]] = {
    "duplicate command id": mutate(
        BASE, ["events", LEDGER_CMD, "command_id"], BASE["events"][LEDGER_CMD + 2]["command_id"]
    ),
    "orphan result": {
        **BASE,
        "events": [
            *BASE["events"],
            {**BASE["events"][LEDGER_RES], "seq": 10_000, "command_id": "ghost"},
        ],
    },
    "undeclared currency": {
        **BASE,
        "chart": [*BASE["chart"], {"account_id": "cad", "kind": "asset", "currency": "CAD"}],
    },
    "contradicted bundled exponent": {
        **BASE,
        "currencies": [*BASE["currencies"], {"code": "JPY", "exponent": 2}],
    },
    "duplicate chart account id": {**BASE, "chart": [*BASE["chart"], BASE["chart"][0]]},
    "orphan tool result": {
        **BASE,
        "events": [
            *BASE["events"],
            {**BASE["events"][TOOL_RES], "seq": 10_000, "call_id": "ghost"},
        ],
    },
    "duplicate tool call": {
        **BASE,
        "events": [*BASE["events"], {**BASE["events"][TOOL_CALL], "seq": 10_000}],
    },
    "oversize aggregate arguments": mutate(
        BASE, ["events", TOOL_CALL, "arguments"], {"a": [0] * 6000, "b": [0] * 6000}
    ),
}


@pytest.mark.parametrize("name", sorted(RUNTIME_ONLY))
def test_cross_event_rules_are_runtime_only_and_documented(name: str) -> None:
    """Pairing and currency resolution span events; JSON Schema cannot express them. The
    schema description lists them as the consumer's job and the models enforce them."""
    schema_ok, models_ok = both_verdicts(RUNTIME_ONLY[name])
    assert (schema_ok, models_ok) == (True, False), (name, schema_ok, models_ok)
    description = load_schema()["description"]
    assert "exactly one `ledger_result`" in description and "resolves" in description
    assert "`chart[].account_id` values are unique" in description
    assert "exactly one `tool_result`" in description and "at most 32" in description


def test_zero_amount_attempt_is_representable_by_both() -> None:
    """A trace records attempts. The ledger rejects a zero amount at replay; the schema and
    models must let the attempt be written down so that rejection can be checked."""
    zero = mutate(BASE, ["events", REFUND_CMD, "command", "money", "amount"], 0)
    assert both_verdicts(zero) == (True, True)


def test_seq_ordering_is_enforced_by_models_and_documented_for_schema() -> None:
    """JSON Schema cannot express strictly-increasing seq; the schema description says so
    and the models enforce it. This is the one deliberate asymmetry, and it is pinned."""
    disordered = mutate(BASE, ["events", 1, "seq"], BASE["events"][0]["seq"])
    schema_ok, models_ok = both_verdicts(disordered)
    assert schema_ok is True and models_ok is False
    assert "strictly increasing" in load_schema()["description"]


# ------------------------------------------------------------ generated traces


@st.composite
def flows(draw: st.DrawFn) -> list[Command]:
    """Random command sequences, including illegal ones, so failures get recorded too."""
    amount = draw(st.integers(1, 10_000))
    commands: list[Command] = [OpenTransaction("open", "t", Money(amount, USD))]
    steps = draw(
        st.lists(st.sampled_from(["auth", "settle", "refund", "retry", "post", "bad"]), max_size=8)
    )
    refunded = 0
    for i, step in enumerate(steps):
        if step == "auth":
            commands.append(Advance(f"a{i}", "t", E.AUTHORIZE))
        elif step == "settle":
            commands.append(Advance(f"s{i}", "t", E.SETTLE, sale(amount)))
        elif step == "refund":
            part = draw(st.integers(1, max(1, amount - refunded)))
            refunded += part
            commands.append(Refund(f"r{i}", "t", Money(part, USD), refund_entry(part)))
        elif step == "retry" and len(commands) > 1:
            commands.append(commands[-1])
        elif step == "post":
            commands.append(Post(f"p{i}", sale(draw(st.integers(1, 100)))))
        else:
            commands.append(Advance(f"x{i}", "t", E.SETTLE))  # missing entry: always fails
    if draw(st.booleans()):
        commands.append(OpenTransaction("zero", "z", Money(0, USD)))  # rejected, recorded
    return commands


@settings(max_examples=60, deadline=None)
@given(flows())
def test_every_recorded_trace_is_schema_valid_round_trips_and_replays(
    commands: list[Command],
) -> None:
    document = recorded(commands)
    validate_document(document)
    trace = parse_trace(document)
    assert json.loads(dump_trace(trace)) == document
    assert replay_trace(trace).consistent


def test_identifier_rules_agree_across_schema_models_and_ledger() -> None:
    """One definition of 'single line', in three places, pinned to each other."""
    from ledgergate.ledger import identifiers
    from ledgergate.trace import models

    ledger_breaks = identifiers.LINE_BREAKS
    assert ledger_breaks == models.LINE_BREAKS
    pattern = load_schema()["$defs"]["identifier"]["pattern"]
    for ch in ledger_breaks:
        probe = mutate(BASE, ["trace_id"], f"a{ch}b")
        assert both_verdicts(probe) == (False, False), repr(ch)
    # and the runtime ledger refuses the same set for its own keys
    from ledgergate.ledger import InvalidIdentifierError
    from ledgergate.ledger.identifiers import require_identifier

    for ch in ledger_breaks:
        with pytest.raises(InvalidIdentifierError):
            require_identifier(f"a{ch}b", "probe")
    assert require_identifier("order 42: refund", "probe") == "order 42: refund"
    assert "\\u2028" in pattern and "\\x85" in pattern
