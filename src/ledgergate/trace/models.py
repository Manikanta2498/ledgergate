# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Typed models for trace schema v1.

These mirror ``schema/trace/v1.json`` field for field. The JSON Schema is the published
contract; these models are how the runtime reads and writes it. A contract test proves
they accept and reject the same documents, so a consumer on another stack can trust the
schema alone and a consumer on this stack gets types.

Every model forbids unknown fields and cannot have a field rebound. Structural fields
(``events``, ``chart``, ``currencies``, ``postings``) are tuples, so the cross-event rules
checked at validation cannot be undone by appending afterwards. Free-form payloads
(``metadata``, ``tags``, tool ``arguments`` and ``result``) are plain JSON containers; they
are validated to be JSON-serializable, finite, and bounded in depth and size, but a caller
holding a reference can still mutate them. Nothing the ledger or replay reads lives there.

A trace with a field this version does not know is not "probably fine"; it is a different
version, and it fails.

What the schema cannot say is enforced here and listed in its description as rules (1)
to (8): ``seq`` strictly increases; every ledger command and every tool call has exactly
one result, after it, with none orphaned; ids are unique; a command's ``call_id`` names a
preceding call; every currency code resolves and never contradicts a bundled exponent;
tool payloads are bounded in aggregate. A document that passes the schema but fails one
of these is rejected by :func:`~ledgergate.trace.io.parse_trace`, so nothing downstream
has to defend against it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from itertools import pairwise
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    TypeAdapter,
    model_validator,
)

from ledgergate.codec import (
    MAX_PAYLOAD_DEPTH,
    MAX_PAYLOAD_NODES,
    MAX_TAGS,
    MAX_TEXT,
    decode_command,
    encode_command,
)
from ledgergate.ledger import (
    CURRENCIES,
    Account,
    AccountType,
    Advance,
    ChartOfAccounts,
    Command,
    Currency,
    EntryDraft,
    Money,
    OpenTransaction,
    Post,
    Posting,
    Refund,
    Side,
)

SCHEMA_VERSION: Literal["1"] = "1"

# Every line break str.splitlines() recognises, plus NUL. Kept in sync with the schema's
# identifier pattern and with ledgergate.ledger.identifiers by a contract test.
LINE_BREAKS = "\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029\x00"
IDENTIFIER_PATTERN = rf"^[^\s{LINE_BREAKS}](?:[^{LINE_BREAKS}]*[^\s{LINE_BREAKS}])?$"

Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=IDENTIFIER_PATTERN)]
ShortText = Annotated[str, Field(max_length=MAX_TEXT)]
LongText = Annotated[str, Field(max_length=65536)]
StringMap = Annotated[dict[str, ShortText], Field(max_length=MAX_TAGS)]
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _to_utc(value: datetime) -> datetime:
    """Normalize to UTC so equal instants serialize identically. ``AwareDatetime`` has
    already refused a naive value by the time this runs. The ledger applies the same
    normalization to every ``posted_at`` it hashes, so a recorded effect fed back on
    replay reproduces the recorded digest exactly."""
    return value.astimezone(UTC)


Timestamp = Annotated[AwareDatetime, AfterValidator(_to_utc)]

Registry = Mapping[str, Currency]


def _check_payload(value: JsonValue) -> JsonValue:
    """Tool arguments and results must be finite JSON of bounded depth and size.

    ``JsonValue`` already refuses anything that is not JSON-shaped, so a stray ``object()``
    fails here rather than at ``dump_trace``. NaN and infinities are JSON-shaped in Python
    but not in JSON, and ``dump_trace`` uses ``allow_nan=False``, so they are refused too.
    """
    nodes = 0
    stack: list[tuple[JsonValue, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > MAX_PAYLOAD_NODES:
            raise ValueError(f"payload exceeds {MAX_PAYLOAD_NODES} nodes")
        if depth > MAX_PAYLOAD_DEPTH:
            raise ValueError(f"payload nesting exceeds {MAX_PAYLOAD_DEPTH}")
        if isinstance(node, float) and (node != node or node in (float("inf"), float("-inf"))):
            raise ValueError("payload contains a non-finite number, which JSON cannot carry")
        if isinstance(node, dict):
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node)
    return value


Payload = Annotated[JsonValue, AfterValidator(_check_payload)]
# The whole arguments object is one payload: the limits apply to the aggregate, not to
# each value separately, or two 6,000-node arguments would pass a 10,000-node limit.
Arguments = Annotated[dict[str, JsonValue], AfterValidator(_check_payload)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ------------------------------------------------------------------ currency


class CurrencyDoc(_Strict):
    code: CurrencyCode
    exponent: Annotated[StrictInt, Field(ge=0, le=6)]

    def to_currency(self) -> Currency:
        return Currency(self.code, self.exponent)

    @classmethod
    def of(cls, cur: Currency) -> CurrencyDoc:
        return cls(code=cur.code, exponent=cur.exponent)


def resolve_currency(code: str, registry: Registry) -> Currency:
    try:
        return registry[code]
    except KeyError:
        raise LookupError(
            f"currency {code!r} is not declared in the trace and is not bundled"
        ) from None


class MoneyDoc(_Strict):
    # StrictInt: 19.0 is a float and is refused, not silently truncated to 19. Not
    # constrained positive: a trace records what was *attempted*, and a zero or negative
    # attempt is exactly what the ledger's rejection, replayed, is meant to prove.
    amount: StrictInt
    currency: CurrencyCode

    def to_money(self, registry: Registry) -> Money:
        return Money(self.amount, resolve_currency(self.currency, registry))

    @classmethod
    def of(cls, money: Money) -> MoneyDoc:
        return cls(amount=money.amount, currency=money.currency.code)


# -------------------------------------------------------------------- entries


class PostingDoc(_Strict):
    account: Identifier
    side: Literal["debit", "credit"]
    money: MoneyDoc

    def to_posting(self, registry: Registry) -> Posting:
        """Raises the ledger's own error for a non-positive amount."""
        return Posting(self.account, Side(self.side), self.money.to_money(registry))

    @classmethod
    def of(cls, posting: Posting) -> PostingDoc:
        return cls(
            account=posting.account_id, side=posting.side.value, money=MoneyDoc.of(posting.money)
        )


class EntryDraftDoc(_Strict):
    postings: Annotated[tuple[PostingDoc, ...], Field(min_length=2, max_length=1000)]
    description: ShortText = ""
    tags: StringMap = Field(default_factory=dict)

    def to_draft(self, registry: Registry) -> EntryDraft:
        """Build the runtime draft. Raises the ledger's own error if it does not balance."""
        return EntryDraft(
            tuple(p.to_posting(registry) for p in self.postings),
            self.description,
            tuple(sorted(self.tags.items())),
        )

    @classmethod
    def of(cls, draft: EntryDraft) -> EntryDraftDoc:
        return cls(
            postings=tuple(PostingDoc.of(p) for p in draft.postings),
            description=draft.description,
            tags=dict(draft.tags),
        )


# ------------------------------------------------------------------- accounts


class AccountDoc(_Strict):
    account_id: Identifier
    kind: Literal["asset", "liability", "equity", "revenue", "expense"]
    currency: CurrencyCode
    allow_negative: StrictBool = True
    name: ShortText = ""

    def to_account(self, registry: Registry) -> Account:
        return Account(
            self.account_id,
            AccountType(self.kind),
            resolve_currency(self.currency, registry),
            self.allow_negative,
            self.name,
        )

    @classmethod
    def of(cls, account: Account) -> AccountDoc:
        return cls(
            account_id=account.account_id,
            kind=account.kind.value,
            currency=account.currency.code,
            allow_negative=account.allow_negative,
            name=account.name,
        )


# ------------------------------------------------------------------- commands

LifecycleEvent = Literal["authorize", "settle", "dispute", "resolve_dispute", "cancel", "fail"]


class PostDoc(_Strict):
    kind: Literal["post"] = "post"
    key: Identifier
    draft: EntryDraftDoc

    def to_command(self, registry: Registry) -> Command:
        return _decode(self, registry)

    def currencies(self) -> Iterator[str]:
        yield from (p.money.currency for p in self.draft.postings)


class ReverseDoc(_Strict):
    kind: Literal["reverse"] = "reverse"
    key: Identifier
    entry_id: Identifier
    description: ShortText = ""

    def to_command(self, registry: Registry) -> Command:
        return _decode(self, registry)

    def currencies(self) -> Iterator[str]:
        yield from ()


class OpenTransactionDoc(_Strict):
    kind: Literal["open_transaction"] = "open_transaction"
    key: Identifier
    transaction_id: Identifier
    amount: MoneyDoc

    def to_command(self, registry: Registry) -> Command:
        return _decode(self, registry)

    def currencies(self) -> Iterator[str]:
        yield self.amount.currency


class AdvanceDoc(_Strict):
    kind: Literal["advance"] = "advance"
    key: Identifier
    transaction_id: Identifier
    event: LifecycleEvent
    entry: EntryDraftDoc | None = None

    def to_command(self, registry: Registry) -> Command:
        return _decode(self, registry)

    def currencies(self) -> Iterator[str]:
        if self.entry is not None:
            yield from (p.money.currency for p in self.entry.postings)


class RefundDoc(_Strict):
    kind: Literal["refund"] = "refund"
    key: Identifier
    transaction_id: Identifier
    money: MoneyDoc
    entry: EntryDraftDoc | None = None

    def to_command(self, registry: Registry) -> Command:
        return _decode(self, registry)

    def currencies(self) -> Iterator[str]:
        yield self.money.currency
        if self.entry is not None:
            yield from (p.money.currency for p in self.entry.postings)


AnyCommandDoc = PostDoc | ReverseDoc | OpenTransactionDoc | AdvanceDoc | RefundDoc
CommandDoc = Annotated[AnyCommandDoc, Field(discriminator="kind")]


_COMMAND_DOC: TypeAdapter[AnyCommandDoc] = TypeAdapter(CommandDoc)


def _decode(doc: _Strict, registry: Registry) -> Command:
    """Every ``*Doc.to_command`` goes through the one codec, so a trace and a journal row
    decode a command identically. The core's own constructors raise their own errors."""
    return decode_command(doc.model_dump(mode="json", exclude_none=True), registry)


def command_doc(command: Command) -> AnyCommandDoc:
    """The document form of a runtime command. Inverse of ``.to_command()``. Produced by
    the shared codec and validated into the typed model, so the two cannot drift."""
    return _COMMAND_DOC.validate_python(encode_command(command))


def command_currencies(command: Command) -> set[Currency]:
    """Every Currency object a runtime command carries, exponents included."""
    found: set[Currency] = set()
    drafts: list[EntryDraft] = []
    match command:
        case Post(_, draft):
            drafts.append(draft)
        case OpenTransaction(_, _, amount):
            found.add(amount.currency)
        case Advance(_, _, _, entry) if entry is not None:
            drafts.append(entry)
        case Refund(_, _, money, entry):
            found.add(money.currency)
            if entry is not None:
                drafts.append(entry)
    for draft in drafts:
        found.update(p.currency for p in draft.postings)
    return found


# --------------------------------------------------------------------- events


class ErrorDoc(_Strict):
    type: Annotated[str, Field(min_length=1, max_length=256)]
    message: ShortText


class _Event(_Strict):
    seq: Annotated[StrictInt, Field(ge=1)]
    at: Timestamp


class MessageEvent(_Event):
    type: Literal["message"] = "message"
    role: Literal["system", "user", "assistant", "tool"]
    content: LongText


class ToolCallEvent(_Event):
    type: Literal["tool_call"] = "tool_call"
    call_id: Identifier
    tool: Identifier
    arguments: Arguments
    idempotency_key: Identifier | None = None


class ToolResultEvent(_Event):
    """Shape depends on ``ok``, like ``ledger_result``: a failure says why, a success
    does not carry an error."""

    type: Literal["tool_result"] = "tool_result"
    call_id: Identifier
    ok: StrictBool
    result: Payload = None
    error: ErrorDoc | None = None

    @model_validator(mode="after")
    def _shape(self) -> ToolResultEvent:
        if self.ok and self.error is not None:
            raise ValueError("successful tool_result must not carry an error")
        if not self.ok and self.error is None:
            raise ValueError("failed tool_result requires error")
        return self


class LedgerCommandEvent(_Event):
    type: Literal["ledger_command"] = "ledger_command"
    command_id: Identifier
    call_id: Identifier | None = None
    command: CommandDoc


class LedgerResultEvent(_Event):
    """Shape depends on ``ok``; see the schema's ``ledger_result_body`` description."""

    type: Literal["ledger_result"] = "ledger_result"
    command_id: Identifier
    ok: StrictBool
    replayed: StrictBool | None = None
    error: ErrorDoc | None = None
    head: Sha256 | None = None
    sequence: Annotated[StrictInt, Field(ge=0)] | None = None
    entry_id: Identifier | None = None
    posted_at: Timestamp | None = None

    @model_validator(mode="after")
    def _shape(self) -> LedgerResultEvent:
        if self.head is None or self.sequence is None:
            raise ValueError("ledger_result requires head and sequence")
        if self.ok:
            if self.replayed is None:
                raise ValueError("successful ledger_result requires replayed")
            if self.error is not None:
                raise ValueError("successful ledger_result must not carry an error")
            if (self.entry_id is None) != (self.posted_at is None):
                raise ValueError("entry_id and posted_at must be present together")
            if self.replayed and self.entry_id is not None:
                raise ValueError("a replayed command appends nothing; entry_id must be absent")
        else:
            if self.error is None:
                raise ValueError("failed ledger_result requires error")
            if self.replayed is not None or self.entry_id is not None or self.posted_at is not None:
                raise ValueError(
                    "failed ledger_result must not carry replayed, entry_id or posted_at"
                )
        return self


AnyEvent = MessageEvent | ToolCallEvent | ToolResultEvent | LedgerCommandEvent | LedgerResultEvent
Event = Annotated[AnyEvent, Field(discriminator="type")]


# ---------------------------------------------------------------------- trace


class AgentDoc(_Strict):
    name: Identifier
    model: ShortText | None = None
    framework: ShortText | None = None
    version: ShortText | None = None


class Trace(_Strict):
    schema_version: Literal["1"] = SCHEMA_VERSION
    trace_id: Identifier
    scenario_id: Identifier | None = None
    agent: AgentDoc
    started_at: Timestamp
    ended_at: Timestamp | None = None
    currencies: Annotated[tuple[CurrencyDoc, ...], Field(max_length=1000)] | None = None
    chart: Annotated[tuple[AccountDoc, ...], Field(max_length=10000)] | None = None
    events: Annotated[tuple[Event, ...], Field(max_length=100000)]
    metadata: StringMap = Field(default_factory=dict)

    @model_validator(mode="after")
    def _semantics(self) -> Trace:
        """The rules the schema's description lists as the consumer's job."""
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("ended_at precedes started_at")
        if any(b.seq <= a.seq for a, b in pairwise(self.events)):
            raise ValueError("event seq must be strictly increasing")

        account_ids = [a.account_id for a in self.chart or []]
        if len(set(account_ids)) != len(account_ids):
            raise ValueError("chart account ids must be unique")

        commands = [e for e in self.events if isinstance(e, LedgerCommandEvent)]
        results = [e for e in self.events if isinstance(e, LedgerResultEvent)]
        command_seq = {c.command_id: c.seq for c in commands}
        if len(command_seq) != len(commands):
            raise ValueError("ledger_command ids must be unique")
        result_seq = {r.command_id: r.seq for r in results}
        if len(result_seq) != len(results):
            raise ValueError("each ledger_command may have only one ledger_result")
        if orphans := sorted(result_seq.keys() - command_seq.keys()):
            raise ValueError(f"ledger_result without a command: {orphans}")
        if unanswered := sorted(command_seq.keys() - result_seq.keys()):
            raise ValueError(f"ledger_command without a result: {unanswered}")
        if early := sorted(c for c, s in command_seq.items() if result_seq[c] <= s):
            raise ValueError(f"ledger_result precedes its command: {early}")

        # Tool calls follow the same discipline as ledger commands: one call, one result,
        # result after call, nothing orphaned. A call the run abandoned is recorded as a
        # failed result with an error (a timeout is an outcome), not as a missing one.
        calls = [e for e in self.events if isinstance(e, ToolCallEvent)]
        tool_results = [e for e in self.events if isinstance(e, ToolResultEvent)]
        call_seq = {c.call_id: c.seq for c in calls}
        if len(call_seq) != len(calls):
            raise ValueError("tool_call ids must be unique")
        tool_result_seq = {r.call_id: r.seq for r in tool_results}
        if len(tool_result_seq) != len(tool_results):
            raise ValueError("each tool_call may have only one tool_result")
        if orphans := sorted(tool_result_seq.keys() - call_seq.keys()):
            raise ValueError(f"tool_result without a call: {orphans}")
        if unanswered := sorted(call_seq.keys() - tool_result_seq.keys()):
            raise ValueError(f"tool_call without a result: {unanswered}")
        if early := sorted(c for c, s in call_seq.items() if tool_result_seq[c] <= s):
            raise ValueError(f"tool_result precedes its call: {early}")
        for command_event in commands:
            cid = command_event.call_id
            if cid is not None and call_seq.get(cid, command_event.seq) >= command_event.seq:
                raise ValueError(
                    f"ledger_command {command_event.command_id} references call {cid!r},"
                    " which does not precede it"
                )

        # Currencies: everything referenced must resolve, and declarations must not
        # contradict the bundled table (a trace cannot redefine what a US cent is).
        declared: dict[str, int] = {}
        for c in self.currencies or []:
            if c.code in declared:
                raise ValueError(f"currency {c.code} declared more than once")
            if c.code in CURRENCIES and CURRENCIES[c.code].exponent != c.exponent:
                raise ValueError(
                    f"currency {c.code} declared with exponent {c.exponent},"
                    f" bundled exponent is {CURRENCIES[c.code].exponent}"
                )
            declared[c.code] = c.exponent
        known = set(declared) | set(CURRENCIES)
        used = {a.currency for a in self.chart or []}
        for command_event in commands:
            used.update(command_event.command.currencies())
        if unknown := sorted(used - known):
            raise ValueError(f"currencies used but not declared and not bundled: {unknown}")
        return self

    # ------------------------------------------------------------- helpers

    def registry(self) -> Registry:
        """Bundled currencies plus this trace's declarations."""
        out: dict[str, Currency] = dict(CURRENCIES)
        out.update((c.code, c.to_currency()) for c in self.currencies or [])
        return out

    def chart_of_accounts(self) -> ChartOfAccounts:
        if self.chart is None:
            raise ValueError("trace carries no chart of accounts; commands cannot be replayed")
        registry = self.registry()
        return ChartOfAccounts(a.to_account(registry) for a in self.chart)

    def commands(self) -> list[LedgerCommandEvent]:
        """Every ledger command event, in order. Conversion is the caller's step, because
        a schema-valid command can be one the ledger rejects, and that rejection is data."""
        return [e for e in self.events if isinstance(e, LedgerCommandEvent)]

    def results(self) -> dict[str, LedgerResultEvent]:
        """Ledger results indexed by command id. Validation guarantees one per command."""
        return {e.command_id: e for e in self.events if isinstance(e, LedgerResultEvent)}
