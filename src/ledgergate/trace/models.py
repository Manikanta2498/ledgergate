# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Typed models for trace schema v1.

These mirror ``schema/trace/v1.json`` field for field. The JSON Schema is the published
contract; these models are how the runtime reads and writes it. A contract test proves
they accept and reject the same documents, so a consumer on another stack can trust the
schema alone and a consumer on this stack gets types.

Every model is frozen and forbids unknown fields. A trace with a field this version does
not know is not "probably fine"; it is a different version, and it fails.
"""

from __future__ import annotations

from datetime import datetime
from itertools import pairwise
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from ledgergate.ledger import (
    Account,
    AccountType,
    Advance,
    ChartOfAccounts,
    Command,
    EntryDraft,
    Money,
    OpenTransaction,
    Post,
    Posting,
    Refund,
    Reverse,
    Side,
    TransactionEvent,
    currency,
)

SCHEMA_VERSION: Literal["1"] = "1"

Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^\S(?:.*\S)?$")]
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------- money


class MoneyDoc(_Strict):
    # StrictInt: 19.0 is a float and is refused, not silently truncated to 19.
    amount: StrictInt
    currency: CurrencyCode

    def to_money(self) -> Money:
        return Money(self.amount, currency(self.currency))

    @classmethod
    def of(cls, money: Money) -> MoneyDoc:
        return cls(amount=money.amount, currency=money.currency.code)


class PositiveMoneyDoc(MoneyDoc):
    amount: Annotated[StrictInt, Field(ge=1)]


# -------------------------------------------------------------------- entries


class PostingDoc(_Strict):
    account: Identifier
    side: Literal["debit", "credit"]
    money: PositiveMoneyDoc

    def to_posting(self) -> Posting:
        return Posting(self.account, Side(self.side), self.money.to_money())

    @classmethod
    def of(cls, posting: Posting) -> PostingDoc:
        return cls(
            account=posting.account_id,
            side=posting.side.value,
            money=PositiveMoneyDoc.of(posting.money),
        )


class EntryDraftDoc(_Strict):
    postings: Annotated[list[PostingDoc], Field(min_length=2)]
    description: str = ""
    tags: dict[str, str] = Field(default_factory=dict)

    def to_draft(self) -> EntryDraft:
        """Build the runtime draft. Raises the ledger's own error if it does not balance."""
        return EntryDraft(
            tuple(p.to_posting() for p in self.postings),
            self.description,
            tuple(sorted(self.tags.items())),
        )

    @classmethod
    def of(cls, draft: EntryDraft) -> EntryDraftDoc:
        return cls(
            postings=[PostingDoc.of(p) for p in draft.postings],
            description=draft.description,
            tags=dict(draft.tags),
        )


# ------------------------------------------------------------------- accounts


class AccountDoc(_Strict):
    account_id: Identifier
    kind: Literal["asset", "liability", "equity", "revenue", "expense"]
    currency: CurrencyCode
    allow_negative: StrictBool = True
    name: str = ""

    def to_account(self) -> Account:
        return Account(
            self.account_id,
            AccountType(self.kind),
            currency(self.currency),
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

    def to_command(self) -> Command:
        return Post(self.key, self.draft.to_draft())


class ReverseDoc(_Strict):
    kind: Literal["reverse"] = "reverse"
    key: Identifier
    entry_id: Identifier
    description: str = ""

    def to_command(self) -> Command:
        return Reverse(self.key, self.entry_id, self.description)


class OpenTransactionDoc(_Strict):
    kind: Literal["open_transaction"] = "open_transaction"
    key: Identifier
    transaction_id: Identifier
    amount: PositiveMoneyDoc

    def to_command(self) -> Command:
        return OpenTransaction(self.key, self.transaction_id, self.amount.to_money())


class AdvanceDoc(_Strict):
    kind: Literal["advance"] = "advance"
    key: Identifier
    transaction_id: Identifier
    event: LifecycleEvent
    entry: EntryDraftDoc | None = None

    def to_command(self) -> Command:
        entry = None if self.entry is None else self.entry.to_draft()
        return Advance(self.key, self.transaction_id, TransactionEvent(self.event), entry)


class RefundDoc(_Strict):
    kind: Literal["refund"] = "refund"
    key: Identifier
    transaction_id: Identifier
    money: PositiveMoneyDoc
    entry: EntryDraftDoc | None = None

    def to_command(self) -> Command:
        entry = None if self.entry is None else self.entry.to_draft()
        return Refund(self.key, self.transaction_id, self.money.to_money(), entry)


CommandDoc = Annotated[
    PostDoc | ReverseDoc | OpenTransactionDoc | AdvanceDoc | RefundDoc,
    Field(discriminator="kind"),
]


def command_doc(
    command: Command,
) -> PostDoc | ReverseDoc | OpenTransactionDoc | AdvanceDoc | RefundDoc:
    """The document form of a runtime command. Inverse of ``.to_command()``."""
    match command:
        case Post(key, draft):
            return PostDoc(key=key, draft=EntryDraftDoc.of(draft))
        case Reverse(key, entry_id, description):
            return ReverseDoc(key=key, entry_id=entry_id, description=description)
        case OpenTransaction(key, transaction_id, amount):
            return OpenTransactionDoc(
                key=key, transaction_id=transaction_id, amount=PositiveMoneyDoc.of(amount)
            )
        case Advance(key, transaction_id, event, entry):
            return AdvanceDoc(
                key=key,
                transaction_id=transaction_id,
                event=event.value,
                entry=None if entry is None else EntryDraftDoc.of(entry),
            )
        case Refund(key, transaction_id, money, entry):
            return RefundDoc(
                key=key,
                transaction_id=transaction_id,
                money=PositiveMoneyDoc.of(money),
                entry=None if entry is None else EntryDraftDoc.of(entry),
            )


# --------------------------------------------------------------------- events


class ErrorDoc(_Strict):
    type: Annotated[str, Field(min_length=1)]
    message: str


class _Event(_Strict):
    seq: Annotated[StrictInt, Field(ge=1)]
    at: datetime

    @field_validator("at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must carry a timezone")
        return value


class MessageEvent(_Event):
    type: Literal["message"] = "message"
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ToolCallEvent(_Event):
    type: Literal["tool_call"] = "tool_call"
    call_id: Identifier
    tool: Identifier
    arguments: dict[str, Any]
    idempotency_key: Identifier | None = None


class ToolResultEvent(_Event):
    type: Literal["tool_result"] = "tool_result"
    call_id: Identifier
    ok: StrictBool
    result: Any = None
    error: ErrorDoc | None = None


class LedgerCommandEvent(_Event):
    type: Literal["ledger_command"] = "ledger_command"
    command_id: Identifier
    call_id: Identifier | None = None
    command: CommandDoc


class LedgerResultEvent(_Event):
    type: Literal["ledger_result"] = "ledger_result"
    command_id: Identifier
    ok: StrictBool
    replayed: StrictBool | None = None
    error: ErrorDoc | None = None
    head: Sha256 | None = None
    sequence: Annotated[StrictInt, Field(ge=0)] | None = None
    # The effects the ledger consumed when it appended an entry. A replayer feeds these
    # back through its Clock and IdGenerator so the recomputed head matches `head`.
    entry_id: Identifier | None = None
    posted_at: datetime | None = None


Event = Annotated[
    MessageEvent | ToolCallEvent | ToolResultEvent | LedgerCommandEvent | LedgerResultEvent,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------- trace


class AgentDoc(_Strict):
    name: Identifier
    model: str | None = None
    framework: str | None = None
    version: str | None = None


class Trace(_Strict):
    schema_version: Literal["1"] = SCHEMA_VERSION
    trace_id: Identifier
    scenario_id: Identifier | None = None
    agent: AgentDoc
    started_at: datetime
    ended_at: datetime | None = None
    chart: list[AccountDoc] | None = None
    events: list[Event]
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("started_at", "ended_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must carry a timezone")
        return value

    @model_validator(mode="after")
    def _ordered(self) -> Trace:
        """`seq` must be strictly increasing. JSON Schema cannot say so; this can."""
        if any(b.seq <= a.seq for a, b in pairwise(self.events)):
            raise ValueError("event seq must be strictly increasing")
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("ended_at precedes started_at")
        return self

    # ------------------------------------------------------------- helpers

    def chart_of_accounts(self) -> ChartOfAccounts:
        if self.chart is None:
            raise ValueError("trace carries no chart of accounts; commands cannot be replayed")
        return ChartOfAccounts(a.to_account() for a in self.chart)

    def commands(self) -> list[tuple[LedgerCommandEvent, Command]]:
        """Every ledger command in the trace, in order, with its runtime form."""
        return [
            (e, e.command.to_command()) for e in self.events if isinstance(e, LedgerCommandEvent)
        ]

    def results(self) -> dict[str, LedgerResultEvent]:
        """Ledger results indexed by command id."""
        return {e.command_id: e for e in self.events if isinstance(e, LedgerResultEvent)}
