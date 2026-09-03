# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""The ledger itself: an immutable value, advanced by commands.

``Ledger.execute(command, clock=..., ids=...)`` is the whole write API. It is a pure
function of ``(state, command, effects)``: it either returns a new ledger or raises, and
the old ledger is untouched either way. That gives atomicity for free -- a refund that
transitions a transaction *and* posts an entry cannot half-apply -- and it is what makes
:func:`replay` reproduce a ledger with identical entries and digests.

Idempotency is part of the state. Every command carries a key; replaying a key with the
same request returns the original result without re-applying, replaying it with a
different request raises. Nothing outside the ledger has to remember what it already did.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from ledgergate.ledger.accounts import Account, ChartOfAccounts, freeze
from ledgergate.ledger.effects import Clock, IdGenerator
from ledgergate.ledger.entries import GENESIS_HASH, Entry, EntryDraft, fingerprint
from ledgergate.ledger.errors import (
    AccountCurrencyMismatchError,
    AlreadyReversedError,
    ChainIntegrityError,
    DuplicateEntryIdError,
    DuplicateTransactionError,
    EntryAmountMismatchError,
    EntryNotAllowedError,
    EntryRequiredError,
    IdempotencyConflictError,
    InsufficientFundsError,
    InvalidAmountError,
    UnknownEntryError,
    UnknownTransactionError,
)
from ledgergate.ledger.identifiers import require_identifier
from ledgergate.ledger.lifecycle import MONETARY_EVENTS, Transaction, TransactionEvent
from ledgergate.ledger.money import Currency, Money

# ------------------------------------------------------------------- commands


@dataclass(frozen=True, slots=True)
class Post:
    """Post a balanced entry."""

    key: str
    draft: EntryDraft


@dataclass(frozen=True, slots=True)
class Reverse:
    """Post the mirror image of an earlier entry. The ledger is append-only; nothing is deleted."""

    key: str
    entry_id: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class OpenTransaction:
    """Start tracking a payment's lifecycle in ``PENDING``."""

    key: str
    transaction_id: str
    amount: Money


@dataclass(frozen=True, slots=True)
class Advance:
    """Move a transaction through a non-refund event.

    ``SETTLE`` moves money and therefore *requires* an ``entry`` whose gross in the
    transaction's currency equals the transaction amount; it is posted atomically with
    the transition. Every other event moves nothing and must not carry an entry.
    """

    key: str
    transaction_id: str
    event: TransactionEvent
    entry: EntryDraft | None = None


@dataclass(frozen=True, slots=True)
class Refund:
    """Refund part or all of a settled transaction.

    ``entry`` is required and must move exactly ``money`` in the transaction's currency;
    it is posted atomically with the transition.
    """

    key: str
    transaction_id: str
    money: Money
    entry: EntryDraft | None = None


Command = Post | Reverse | OpenTransaction | Advance | Refund


def command_fingerprint(command: Command) -> str:
    """The one fingerprint of a command's *request*, excluding its key.

    Idempotency compares this: same key and same fingerprint is a replay, same key and a
    different fingerprint is a conflict. The ledger computes it before executing and the
    durable journal stores the same value, so there is exactly one definition and the two
    cannot disagree. Amounts are encoded as decimal strings; the encoding is the
    length-prefixed one in :func:`ledgergate.ledger.entries.fingerprint`.
    """
    match command:
        case Post(_, draft):
            return fingerprint("post", {"draft": draft.canonical()})
        case Reverse(_, entry_id, description):
            return fingerprint("reverse", {"entry": entry_id, "description": description})
        case OpenTransaction(_, transaction_id, amount):
            return fingerprint(
                "open",
                {"txn": transaction_id, "amount": str(amount.amount), "ccy": amount.currency.code},
            )
        case Advance(_, transaction_id, event, entry):
            return fingerprint(
                "advance",
                {
                    "txn": transaction_id,
                    "event": event.value,
                    "entry": "" if entry is None else entry.canonical(),
                },
            )
        case Refund(_, transaction_id, money, entry):
            return fingerprint(
                "refund",
                {
                    "txn": transaction_id,
                    "amount": str(money.amount),
                    "ccy": money.currency.code,
                    "entry": "" if entry is None else entry.canonical(),
                },
            )


# -------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """What an idempotency key was first used for, and what it produced."""

    fingerprint: str
    entry_id: str | None = None
    transaction_id: str | None = None


@dataclass(frozen=True, slots=True)
class Applied:
    """The outcome of executing one command."""

    ledger: Ledger
    entry: Entry | None = None
    transaction: Transaction | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class TrialBalanceRow:
    account: Account
    debit: Money
    credit: Money

    @property
    def balance(self) -> Money:
        """Balance on the account's normal side; positive is the expected sign."""
        raw = self.debit.amount - self.credit.amount
        return Money(raw * self.account.kind.normal_side.sign, self.account.currency)


@dataclass(frozen=True, slots=True)
class TrialBalance:
    rows: tuple[TrialBalanceRow, ...]

    def totals(self) -> dict[Currency, tuple[Money, Money]]:
        """``{currency: (total debits, total credits)}``."""
        out: dict[Currency, tuple[Money, Money]] = {}
        for row in self.rows:
            cur = row.account.currency
            debit_total, credit_total = out.get(cur, (Money.zero(cur), Money.zero(cur)))
            out[cur] = (debit_total + row.debit, credit_total + row.credit)
        return out

    @property
    def is_balanced(self) -> bool:
        return all(d == c for d, c in self.totals().values())


# --------------------------------------------------------------------- ledger


def _canonical_time(at: datetime) -> datetime:
    """Every timestamp the ledger stores and hashes is UTC.

    The digest covers ``posted_at.isoformat()``. Two clocks reading the same instant in
    different zones would otherwise produce different digests for the same entry, and a
    trace that normalizes to UTC (as the schema requires) could not reproduce the head of
    the ledger it recorded. A naive datetime is refused outright: ``astimezone`` on a
    naive value consults the system's local zone, which is a hidden wall-clock input.
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise InvalidAmountError("Clock.now() must return a timezone-aware datetime")
    return at.astimezone(UTC)


@dataclass(frozen=True, slots=True, eq=False)
class Ledger:
    """An immutable ledger. Every mutation returns a new instance.

    The underscored fields are indexes derived from ``entries``. They are dataclass
    fields so that ``replace`` can carry them forward cheaply, but they are not part of
    the public contract, and :meth:`verify_chain` re-derives and cross-checks them so a
    hand-edited index is detected rather than trusted.
    """

    chart: ChartOfAccounts
    entries: tuple[Entry, ...] = ()
    transactions: Mapping[str, Transaction] = field(default_factory=dict)
    operations: Mapping[str, OperationRecord] = field(default_factory=dict)
    head: str = GENESIS_HASH
    _balances: Mapping[str, int] = field(default_factory=dict, repr=False)
    _reversals: Mapping[str, str] = field(default_factory=dict, repr=False)
    _by_id: Mapping[str, Entry] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # A frozen dataclass only freezes its own attributes. Every mapping is wrapped in
        # a read-only view here, whatever path constructed it, so `ledger.operations.clear()`
        # is a TypeError rather than a way to switch off idempotency.
        for name in ("transactions", "operations", "_balances", "_reversals", "_by_id"):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        object.__setattr__(self, "entries", tuple(self.entries))

    @classmethod
    def empty(cls, chart: ChartOfAccounts) -> Ledger:
        return cls(chart)

    def __eq__(self, other: object) -> bool:
        """Two ledgers are equal when they hold the same history and lifecycle state.

        Derived indexes are excluded on purpose: they are a function of ``entries``, and
        two ledgers replayed from the same commands must compare equal.
        """
        if not isinstance(other, Ledger):
            return NotImplemented
        return (
            self.head == other.head
            and self.entries == other.entries
            and dict(self.transactions) == dict(other.transactions)
            and dict(self.operations) == dict(other.operations)
            and dict(self.chart) == dict(other.chart)
        )

    def __hash__(self) -> int:
        return hash((self.head, self.sequence))

    # ------------------------------------------------------------- reading

    @property
    def sequence(self) -> int:
        """Number of entries posted so far."""
        return len(self.entries)

    def raw_balance(self, account_id: str) -> int:
        """Debit-positive minor units. Zero for a valid account with no postings."""
        self.chart[account_id]
        return self._balances.get(account_id, 0)

    def balance(self, account_id: str) -> Money:
        """Balance on the account's normal side. A liability owed reads positive."""
        account = self.chart[account_id]
        return Money(self.raw_balance(account_id) * account.kind.normal_side.sign, account.currency)

    def entry(self, entry_id: str) -> Entry:
        try:
            return self._by_id[entry_id]
        except KeyError:
            raise UnknownEntryError(entry_id) from None

    def has_entry(self, entry_id: str) -> bool:
        return entry_id in self._by_id

    def entries_for(self, account_id: str) -> tuple[Entry, ...]:
        self.chart[account_id]
        return tuple(e for e in self.entries if account_id in e.draft.account_ids)

    def reversal_of(self, entry_id: str) -> Entry | None:
        """The entry that reversed ``entry_id``, if any."""
        self.entry(entry_id)
        reversing = self._reversals.get(entry_id)
        return None if reversing is None else self._by_id[reversing]

    def transaction(self, transaction_id: str) -> Transaction:
        try:
            return self.transactions[transaction_id]
        except KeyError:
            raise UnknownTransactionError(transaction_id) from None

    def trial_balance(self) -> TrialBalance:
        rows = []
        for account in self.chart.values():
            raw = self._balances.get(account.account_id, 0)
            debit = Money(max(raw, 0), account.currency)
            credit = Money(max(-raw, 0), account.currency)
            rows.append(TrialBalanceRow(account, debit, credit))
        return TrialBalance(tuple(rows))

    def verify_chain(self) -> bool:
        """Recompute the chain *and* every index derived from it, from the entries alone.

        Checks, in order: each entry's sequence number and back-link, each digest, the
        head, and then that the stored balances, id index and reversal index are exactly
        what a fresh fold over the entries produces. A ledger whose balances were edited
        without touching the entries therefore fails here, not only one whose entries
        were edited.

        Transactions and idempotency records are not derivable from entries (a
        transaction can move without posting), so they are outside this check; replaying
        the command log with :func:`replay` is the audit for those.

        Raises :class:`ChainIntegrityError` on the first discrepancy.
        """
        previous = GENESIS_HASH
        balances: dict[str, int] = {}
        by_id: dict[str, Entry] = {}
        reversals: dict[str, str] = {}
        for expected_seq, entry in enumerate(self.entries, start=1):
            if entry.sequence != expected_seq or entry.previous_hash != previous:
                raise ChainIntegrityError(entry.sequence, previous, entry.previous_hash)
            actual = entry.recomputed_digest()
            if actual != entry.digest:
                raise ChainIntegrityError(entry.sequence, entry.digest, actual)
            if entry.entry_id in by_id:
                raise ChainIntegrityError(entry.sequence, "unique id", entry.entry_id)
            by_id[entry.entry_id] = entry
            if entry.reverses is not None:
                reversals[entry.reverses] = entry.entry_id
            for posting in entry.postings:
                balances[posting.account_id] = (
                    balances.get(posting.account_id, 0) + posting.signed_amount
                )
            previous = entry.digest
        if previous != self.head:
            raise ChainIntegrityError(self.sequence, previous, self.head)

        stored = {k: v for k, v in self._balances.items() if v != 0}
        derived = {k: v for k, v in balances.items() if v != 0}
        if stored != derived:
            raise ChainIntegrityError(self.sequence, "balances", "stored balances")
        if dict(self._by_id) != by_id:
            raise ChainIntegrityError(self.sequence, "entry index", "stored index")
        if dict(self._reversals) != reversals:
            raise ChainIntegrityError(self.sequence, "reversal index", "stored index")
        return True

    # ------------------------------------------------------------- writing

    def execute(self, command: Command, *, clock: Clock, ids: IdGenerator) -> Applied:
        """Apply one command. Returns the new ledger and what it produced, or raises."""
        require_identifier(command.key, "idempotency key")
        match command:
            case Post(key, draft):
                return self._post(key, draft, clock=clock, ids=ids)
            case Reverse(key, entry_id, description):
                return self._reverse(key, entry_id, description, clock=clock, ids=ids)
            case OpenTransaction(key, transaction_id, amount):
                return self._open(key, transaction_id, amount)
            case Advance(key, transaction_id, event, entry):
                return self._advance(key, transaction_id, event, entry, clock=clock, ids=ids)
            case Refund(key, transaction_id, money, entry):
                return self._refund(key, transaction_id, money, entry, clock=clock, ids=ids)

    def post(self, draft: EntryDraft, *, key: str, clock: Clock, ids: IdGenerator) -> Applied:
        """Convenience for :class:`Post`."""
        return self.execute(Post(key, draft), clock=clock, ids=ids)

    # ---------------------------------------------------------- internals

    def _replay(self, key: str, print_: str) -> Applied | None:
        """Return the recorded result if ``key`` was seen with this fingerprint."""
        record = self.operations.get(key)
        if record is None:
            return None
        if record.fingerprint != print_:
            raise IdempotencyConflictError(key)
        return Applied(
            self,
            entry=None if record.entry_id is None else self._by_id[record.entry_id],
            transaction=(
                None if record.transaction_id is None else self.transactions[record.transaction_id]
            ),
            replayed=True,
        )

    def _validate(self, draft: EntryDraft) -> dict[str, int]:
        """Check accounts and currencies, then return the balances after the draft."""
        balances = dict(self._balances)
        for posting in draft.postings:
            account = self.chart[posting.account_id]
            if account.currency != posting.currency:
                raise AccountCurrencyMismatchError(
                    account.account_id, account.currency.code, posting.currency.code
                )
            balances[account.account_id] = (
                balances.get(account.account_id, 0) + posting.signed_amount
            )

        for account_id in draft.account_ids:
            account = self.chart[account_id]
            if account.allow_negative:
                continue
            side = account.kind.normal_side.sign
            after = balances.get(account_id, 0) * side
            if after < 0:
                before = self._balances.get(account_id, 0) * side
                raise InsufficientFundsError(account_id, before, after - before)
        return balances

    def _append(
        self,
        key: str,
        print_: str,
        draft: EntryDraft,
        *,
        reverses: str | None,
        clock: Clock,
        ids: IdGenerator,
        transactions: Mapping[str, Transaction] | None = None,
        transaction_id: str | None = None,
    ) -> Applied:
        """Post ``draft`` as the next entry. Effects are consumed only after validation."""
        balances = self._validate(draft)
        sequence = self.sequence + 1
        # The IdGenerator promises fresh, usable ids; the ledger does not take its word.
        entry_id = require_identifier(ids.next_id(), "generated entry id")
        if entry_id in self._by_id:
            raise DuplicateEntryIdError(entry_id)
        posted_at = _canonical_time(clock.now())
        digest = Entry.compute_digest(
            entry_id=entry_id,
            sequence=sequence,
            posted_at=posted_at,
            idempotency_key=key,
            draft=draft,
            previous_hash=self.head,
            reverses=reverses,
        )
        entry = Entry(entry_id, sequence, posted_at, key, draft, self.head, digest, reverses)

        reversals = dict(self._reversals)
        if reverses is not None:
            reversals[reverses] = entry_id
        new = replace(
            self,
            entries=(*self.entries, entry),
            transactions=self.transactions if transactions is None else transactions,
            operations={**self.operations, key: OperationRecord(print_, entry_id, transaction_id)},
            head=digest,
            _balances=balances,
            _reversals=reversals,
            _by_id={**self._by_id, entry_id: entry},
        )
        txn = None if transaction_id is None else new.transactions[transaction_id]
        return Applied(new, entry=entry, transaction=txn)

    def _post(self, key: str, draft: EntryDraft, *, clock: Clock, ids: IdGenerator) -> Applied:
        print_ = command_fingerprint(Post(key, draft))
        if replayed := self._replay(key, print_):
            return replayed
        return self._append(key, print_, draft, reverses=None, clock=clock, ids=ids)

    def _reverse(
        self, key: str, entry_id: str, description: str, *, clock: Clock, ids: IdGenerator
    ) -> Applied:
        print_ = command_fingerprint(Reverse(key, entry_id, description))
        if replayed := self._replay(key, print_):
            return replayed
        original = self.entry(entry_id)
        if (by := self._reversals.get(entry_id)) is not None:
            raise AlreadyReversedError(entry_id, by)
        draft = original.draft.reversed(description)
        return self._append(key, print_, draft, reverses=entry_id, clock=clock, ids=ids)

    def _open(self, key: str, transaction_id: str, amount: Money) -> Applied:
        print_ = command_fingerprint(OpenTransaction(key, transaction_id, amount))
        if replayed := self._replay(key, print_):
            return replayed
        if transaction_id in self.transactions:
            raise DuplicateTransactionError(transaction_id)
        txn = Transaction(transaction_id, amount)
        new = replace(
            self,
            transactions={**self.transactions, transaction_id: txn},
            operations={
                **self.operations,
                key: OperationRecord(print_, transaction_id=transaction_id),
            },
        )
        return Applied(new, transaction=txn)

    def _advance(
        self,
        key: str,
        transaction_id: str,
        event: TransactionEvent,
        entry: EntryDraft | None,
        *,
        clock: Clock,
        ids: IdGenerator,
    ) -> Applied:
        print_ = command_fingerprint(Advance(key, transaction_id, event, entry))
        if replayed := self._replay(key, print_):
            return replayed
        current = self.transaction(transaction_id)
        txn = current.advance(event)
        if event in MONETARY_EVENTS:
            _require_entry_moving(entry, txn.transaction_id, event, current.amount)
        elif entry is not None:
            raise EntryNotAllowedError(transaction_id, event.value)
        return self._commit_transaction(key, print_, txn, entry, clock=clock, ids=ids)

    def _refund(
        self,
        key: str,
        transaction_id: str,
        money: Money,
        entry: EntryDraft | None,
        *,
        clock: Clock,
        ids: IdGenerator,
    ) -> Applied:
        print_ = command_fingerprint(Refund(key, transaction_id, money, entry))
        if replayed := self._replay(key, print_):
            return replayed
        txn = self.transaction(transaction_id).refund(money)
        _require_entry_moving(entry, transaction_id, TransactionEvent.REFUND, money)
        return self._commit_transaction(key, print_, txn, entry, clock=clock, ids=ids)

    def _commit_transaction(
        self,
        key: str,
        print_: str,
        txn: Transaction,
        entry: EntryDraft | None,
        *,
        clock: Clock,
        ids: IdGenerator,
    ) -> Applied:
        """Store the advanced transaction and, if given, its entry, as one atomic step."""
        transactions = {**self.transactions, txn.transaction_id: txn}
        if entry is None:
            new = replace(
                self,
                transactions=transactions,
                operations={
                    **self.operations,
                    key: OperationRecord(print_, transaction_id=txn.transaction_id),
                },
            )
            return Applied(new, transaction=txn)
        return self._append(
            key,
            print_,
            entry,
            reverses=None,
            clock=clock,
            ids=ids,
            transactions=transactions,
            transaction_id=txn.transaction_id,
        )


def _require_entry_moving(
    entry: EntryDraft | None, transaction_id: str, event: TransactionEvent, amount: Money
) -> None:
    """A monetary event needs an entry, and that entry must move exactly ``amount``.

    "Move" means the gross debited in the transaction's currency. This ties the lifecycle
    to the books: a 100.00 settlement cannot be backed by a 0.01 entry, and a 30.00
    refund cannot be recorded against an unrelated fees posting. Which *accounts* the
    money moves between is not checked here; that is the invariants layer's job.
    """
    if entry is None:
        raise EntryRequiredError(transaction_id, event.value)
    moved = entry.gross(amount.currency)
    if moved != amount:
        raise EntryAmountMismatchError(transaction_id, str(amount), str(moved))


# --------------------------------------------------------------------- replay


def replay(
    chart: ChartOfAccounts, commands: Iterable[Command], *, clock: Clock, ids: IdGenerator
) -> Ledger:
    """Fold commands over an empty ledger. Same inputs, same effects, same ledger."""
    ledger = Ledger.empty(chart)
    for command in commands:
        ledger = ledger.execute(command, clock=clock, ids=ids).ledger
    return ledger
