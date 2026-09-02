# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Typed failures raised by the ledger core.

Every error is a subclass of :class:`LedgerError` so callers can catch the family, and
every error carries enough structure to be asserted on in a test or rendered in a report
without parsing its message.
"""

from __future__ import annotations


class LedgerError(Exception):
    """Base class for every failure the ledger core can raise."""


class InvalidIdentifierError(LedgerError):
    """A key or id is empty, padded, multi-line or otherwise unusable as a mapping key."""

    def __init__(self, what: str, value: str, reason: str) -> None:
        self.what, self.value, self.reason = what, value, reason
        super().__init__(f"{what} {value!r} {reason}")


# ------------------------------------------------------------------ money


class MoneyError(LedgerError):
    """A monetary value or operation is invalid."""


class InvalidAmountError(MoneyError):
    """An amount is not an integer number of minor units, or is out of range."""


class CurrencyMismatchError(MoneyError):
    """Two values in different currencies were combined without a conversion."""

    def __init__(self, left: str, right: str) -> None:
        self.left, self.right = left, right
        super().__init__(f"cannot combine {left} with {right} without an explicit conversion")


class MissingRateError(MoneyError):
    """The rate source has no rate for the requested currency pair."""

    def __init__(self, base: str, quote: str) -> None:
        self.base, self.quote = base, quote
        super().__init__(f"no rate available for {base}->{quote}")


# --------------------------------------------------------------- accounts


class AccountError(LedgerError):
    """A chart-of-accounts problem."""


class UnknownAccountError(AccountError):
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        super().__init__(f"unknown account {account_id!r}")


class DuplicateAccountError(AccountError):
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        super().__init__(f"account {account_id!r} is defined more than once")


class AccountCurrencyMismatchError(AccountError):
    def __init__(self, account_id: str, expected: str, actual: str) -> None:
        self.account_id, self.expected, self.actual = account_id, expected, actual
        super().__init__(f"account {account_id!r} holds {expected}, posting is in {actual}")


class InsufficientFundsError(AccountError):
    """A posting would take an account that forbids it below zero."""

    def __init__(self, account_id: str, balance: int, attempted: int) -> None:
        self.account_id, self.balance, self.attempted = account_id, balance, attempted
        super().__init__(
            f"account {account_id!r} would go to {balance + attempted} minor units"
            f" (balance {balance}, posting {attempted}) and does not allow a negative balance"
        )


# ---------------------------------------------------------------- entries


class EntryError(LedgerError):
    """A journal entry is malformed."""


class EmptyEntryError(EntryError):
    def __init__(self) -> None:
        super().__init__("an entry needs at least two postings")


class UnbalancedEntryError(EntryError):
    """Debits do not equal credits in at least one currency.

    ``imbalance`` maps currency code to the net debit-minus-credit in minor units.
    """

    def __init__(self, imbalance: dict[str, int]) -> None:
        self.imbalance = dict(imbalance)
        detail = ", ".join(f"{code}: {net:+d}" for code, net in sorted(self.imbalance.items()))
        super().__init__(f"entry does not balance ({detail})")


class UnknownEntryError(EntryError):
    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id
        super().__init__(f"unknown entry {entry_id!r}")


class DuplicateEntryIdError(EntryError):
    """The injected IdGenerator returned an id the ledger already holds."""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id
        super().__init__(f"entry id {entry_id!r} was already issued; the IdGenerator is not fresh")


class AlreadyReversedError(EntryError):
    def __init__(self, entry_id: str, reversed_by: str) -> None:
        self.entry_id, self.reversed_by = entry_id, reversed_by
        super().__init__(f"entry {entry_id!r} was already reversed by {reversed_by!r}")


class ChainIntegrityError(EntryError):
    """The hash chain does not recompute; the ledger has been tampered with or corrupted."""

    def __init__(self, sequence: int, expected: str, actual: str) -> None:
        self.sequence, self.expected, self.actual = sequence, expected, actual
        super().__init__(
            f"ledger integrity broken at sequence {sequence}:"
            f" expected {expected[:16]!r}, found {actual[:16]!r}"
        )


# ------------------------------------------------------------ idempotency


class IdempotencyConflictError(LedgerError):
    """A key was replayed with a different request than the one it first recorded.

    Replaying the *same* request is not an error; it returns the original result. Reusing
    a key for a *different* request is exactly the bug idempotency exists to catch.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"idempotency key {key!r} was already used for a different request")


# -------------------------------------------------------------- lifecycle


class TransactionError(LedgerError):
    """A payment-lifecycle problem."""


class UnknownTransactionError(TransactionError):
    def __init__(self, transaction_id: str) -> None:
        self.transaction_id = transaction_id
        super().__init__(f"unknown transaction {transaction_id!r}")


class DuplicateTransactionError(TransactionError):
    def __init__(self, transaction_id: str) -> None:
        self.transaction_id = transaction_id
        super().__init__(f"transaction {transaction_id!r} already exists")


class IllegalTransitionError(TransactionError):
    def __init__(self, transaction_id: str, status: str, event: str) -> None:
        self.transaction_id, self.status, self.event = transaction_id, status, event
        super().__init__(f"transaction {transaction_id!r} in {status} cannot accept {event}")


class EntryRequiredError(TransactionError):
    """A money-moving event was issued without the journal entry that moves the money."""

    def __init__(self, transaction_id: str, event: str) -> None:
        self.transaction_id, self.event = transaction_id, event
        super().__init__(f"{event} on transaction {transaction_id!r} requires a journal entry")


class EntryNotAllowedError(TransactionError):
    """A non-monetary event was given a journal entry; nothing should move on it."""

    def __init__(self, transaction_id: str, event: str) -> None:
        self.transaction_id, self.event = transaction_id, event
        super().__init__(
            f"{event} on transaction {transaction_id!r} moves no money; drop the entry"
        )


class EntryAmountMismatchError(TransactionError):
    """The journal entry does not move the amount the lifecycle event says it does."""

    def __init__(self, transaction_id: str, expected: str, actual: str) -> None:
        self.transaction_id, self.expected, self.actual = transaction_id, expected, actual
        super().__init__(
            f"entry for transaction {transaction_id!r} moves {actual}, event says {expected}"
        )


class RefundExceedsSettledError(TransactionError):
    def __init__(self, transaction_id: str, remaining: int, attempted: int) -> None:
        self.transaction_id, self.remaining, self.attempted = transaction_id, remaining, attempted
        super().__init__(
            f"refund of {attempted} exceeds the {remaining} still refundable"
            f" on transaction {transaction_id!r}"
        )
