# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""The payment state machine.

Every legal move is a row in :data:`TRANSITIONS`. Anything not in the table is illegal,
so ``PENDING -> REFUNDED`` fails by construction rather than by someone remembering to
check. Refunds are the one event whose destination depends on data: a refund that brings
cumulative refunds to the full amount lands in ``REFUNDED``, anything less lands in
``PARTIALLY_REFUNDED``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType

from ledgergate.ledger.errors import (
    CurrencyMismatchError,
    IllegalTransitionError,
    InvalidAmountError,
    RefundExceedsSettledError,
)
from ledgergate.ledger.identifiers import require_identifier
from ledgergate.ledger.money import Money


class TransactionStatus(Enum):
    PENDING = "pending"
    AUTHORIZED = "authorized"
    SETTLED = "settled"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    DISPUTED = "disputed"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL


class TransactionEvent(Enum):
    AUTHORIZE = "authorize"
    SETTLE = "settle"
    REFUND = "refund"
    DISPUTE = "dispute"
    RESOLVE_DISPUTE = "resolve_dispute"
    CANCEL = "cancel"
    FAIL = "fail"


S, E = TransactionStatus, TransactionEvent

TRANSITIONS: Mapping[tuple[TransactionStatus, TransactionEvent], TransactionStatus] = (
    MappingProxyType(
        {
            (S.PENDING, E.AUTHORIZE): S.AUTHORIZED,
            (S.PENDING, E.CANCEL): S.CANCELLED,
            (S.PENDING, E.FAIL): S.FAILED,
            (S.AUTHORIZED, E.SETTLE): S.SETTLED,
            (S.AUTHORIZED, E.CANCEL): S.CANCELLED,
            (S.AUTHORIZED, E.FAIL): S.FAILED,
            # Two events have data-dependent destinations, and the table records only that they
            # are *permitted*. REFUND lands in REFUNDED or PARTIALLY_REFUNDED depending on the
            # running total; RESOLVE_DISPUTE returns to whichever of SETTLED or PARTIALLY_REFUNDED
            # the transaction was in before the dispute. See `Transaction.advance` / `refund`.
            (S.SETTLED, E.REFUND): S.PARTIALLY_REFUNDED,
            (S.PARTIALLY_REFUNDED, E.REFUND): S.PARTIALLY_REFUNDED,
            (S.SETTLED, E.DISPUTE): S.DISPUTED,
            (S.PARTIALLY_REFUNDED, E.DISPUTE): S.DISPUTED,
            (S.DISPUTED, E.RESOLVE_DISPUTE): S.SETTLED,
        }
    )
)

TERMINAL: frozenset[TransactionStatus] = frozenset({S.REFUNDED, S.CANCELLED, S.FAILED})

MONETARY_EVENTS: frozenset[TransactionEvent] = frozenset({E.SETTLE, E.REFUND})
"""Events that move money and therefore must be accompanied by a journal entry."""


def allowed_events(status: TransactionStatus) -> frozenset[TransactionEvent]:
    return frozenset(event for (from_status, event) in TRANSITIONS if from_status is status)


def transition(
    transaction_id: str, status: TransactionStatus, event: TransactionEvent
) -> TransactionStatus:
    """Return the status after ``event``, or raise :class:`IllegalTransitionError`."""
    try:
        return TRANSITIONS[(status, event)]
    except KeyError:
        raise IllegalTransitionError(transaction_id, status.value, event.value) from None


@dataclass(frozen=True, slots=True)
class Transaction:
    """A payment's lifecycle record. Ledger effects live in entries; this is the state."""

    transaction_id: str
    amount: Money
    status: TransactionStatus = TransactionStatus.PENDING
    refunded_minor: int = 0

    def __post_init__(self) -> None:
        require_identifier(self.transaction_id, "transaction id")
        if not self.amount.is_positive:
            raise InvalidAmountError(f"transaction amount must be positive, got {self.amount}")
        if isinstance(self.refunded_minor, bool) or not isinstance(self.refunded_minor, int):
            raise InvalidAmountError("refunded amount must be an int of minor units")
        if not 0 <= self.refunded_minor <= self.amount.amount:
            raise InvalidAmountError("refunded amount must be between zero and the amount")
        # The status and the refunded total must tell the same story. The constructor is
        # public, so this is checked here and not only on the transition paths.
        total, refunded = self.amount.amount, self.refunded_minor
        consistent = {
            S.REFUNDED: refunded == total,
            S.PARTIALLY_REFUNDED: 0 < refunded < total,
            S.DISPUTED: refunded < total,
        }.get(self.status, refunded == 0)
        if not consistent:
            raise InvalidAmountError(
                f"status {self.status.value} is inconsistent with {refunded}/{total} refunded"
            )

    @property
    def refunded(self) -> Money:
        return Money(self.refunded_minor, self.amount.currency)

    @property
    def refundable(self) -> Money:
        return self.amount - self.refunded

    @property
    def settled_status(self) -> TransactionStatus:
        """SETTLED or PARTIALLY_REFUNDED, according to what has been refunded so far."""
        return S.PARTIALLY_REFUNDED if self.refunded_minor else S.SETTLED

    def advance(self, event: TransactionEvent) -> Transaction:
        """Apply a non-refund event. Refunds carry an amount; use :meth:`refund`."""
        if event is TransactionEvent.REFUND:
            raise IllegalTransitionError(
                self.transaction_id, self.status.value, "refund without an amount"
            )
        status = transition(self.transaction_id, self.status, event)
        if event is TransactionEvent.RESOLVE_DISPUTE:
            # A dispute does not un-refund anything: resolving it must not erase the fact
            # that part of the amount is already gone.
            status = self.settled_status
        return replace(self, status=status)

    def refund(self, money: Money) -> Transaction:
        """Apply a refund, deciding PARTIALLY_REFUNDED vs REFUNDED from the running total."""
        transition(self.transaction_id, self.status, TransactionEvent.REFUND)
        if money.currency != self.amount.currency:
            raise CurrencyMismatchError(self.amount.currency.code, money.currency.code)
        if not money.is_positive or money > self.refundable:
            raise RefundExceedsSettledError(
                self.transaction_id, self.refundable.amount, money.amount
            )
        cumulative = self.refunded_minor + money.amount
        status = S.REFUNDED if cumulative == self.amount.amount else S.PARTIALLY_REFUNDED
        return replace(self, status=status, refunded_minor=cumulative)
