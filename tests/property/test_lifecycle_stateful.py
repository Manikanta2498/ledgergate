"""A model-based test of the payment lifecycle driven through the ledger.

Hypothesis drives a transaction through random events. A tiny independent model tracks
what *should* be true; the invariants compare the ledger against it after every step.
This is the test that catches ``PENDING -> REFUNDED`` and the double refund, not because
someone wrote that case down but because the machine will eventually try it.
"""

from __future__ import annotations

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

from ledgergate.ledger import (
    EPOCH,
    TERMINAL,
    USD,
    Account,
    AccountType,
    Advance,
    ChartOfAccounts,
    Command,
    EntryDraft,
    IllegalTransitionError,
    Ledger,
    Money,
    OpenTransaction,
    Refund,
    RefundExceedsSettledError,
    SequentialIds,
    SteppingClock,
    TransactionEvent,
    TransactionStatus,
    allowed_events,
    credit,
    debit,
)

E, S = TransactionEvent, TransactionStatus

CHART = ChartOfAccounts(
    [
        Account("cash", AccountType.ASSET, USD),
        Account("revenue", AccountType.REVENUE, USD),
    ]
)


def settlement(amount: int) -> EntryDraft:
    return EntryDraft.of(debit("cash", Money(amount, USD)), credit("revenue", Money(amount, USD)))


def refund_entry(amount: int) -> EntryDraft:
    return EntryDraft.of(debit("revenue", Money(amount, USD)), credit("cash", Money(amount, USD)))


class PaymentLifecycle(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.clock = SteppingClock(EPOCH)
        self.ids = SequentialIds()
        self.ledger = Ledger.empty(CHART)
        self.amount = 0
        self.model_status = S.PENDING
        self.model_refunded = 0
        self.model_cash = 0
        self.keys = 0
        self.last_applied: Command | None = None

    def key(self) -> str:
        self.keys += 1
        return f"k{self.keys}"

    def apply(self, cmd: Command) -> None:
        self.ledger = self.ledger.execute(cmd, clock=self.clock, ids=self.ids).ledger
        self.last_applied = cmd

    @initialize(amount=st.integers(min_value=1, max_value=10_000))
    def open(self, amount: int) -> None:
        self.amount = amount
        self.apply(OpenTransaction(self.key(), "t", Money(amount, USD)))

    # ---------------------------------------------------------------- rules
    #
    # Legal moves carry preconditions so Hypothesis only offers them when the model says
    # they apply. Without that, `cancel` and `fail` are on the menu from step one and a
    # quarter of all runs die in a terminal state before ever reaching a refund, let
    # alone a refund-then-dispute-then-resolve. Illegal moves are probed by one dedicated
    # rule that picks any event and demands a rejection.

    def _in(self, *statuses: TransactionStatus) -> bool:
        return self.model_status in statuses

    @precondition(lambda self: self._in(S.PENDING))
    @rule()
    def authorize(self) -> None:
        self.apply(Advance(self.key(), "t", E.AUTHORIZE))
        self.model_status = S.AUTHORIZED

    @precondition(lambda self: self._in(S.AUTHORIZED))
    @rule()
    def settle(self) -> None:
        self.apply(Advance(self.key(), "t", E.SETTLE, settlement(self.amount)))
        self.model_status = S.SETTLED
        self.model_cash += self.amount

    @precondition(lambda self: self._in(S.PENDING, S.AUTHORIZED))
    @rule(event=st.sampled_from([E.CANCEL, E.FAIL]))
    def abandon(self, event: TransactionEvent) -> None:
        self.apply(Advance(self.key(), "t", event))
        self.model_status = S.CANCELLED if event is E.CANCEL else S.FAILED

    @precondition(lambda self: self._in(S.SETTLED, S.PARTIALLY_REFUNDED))
    @rule()
    def dispute(self) -> None:
        self.apply(Advance(self.key(), "t", E.DISPUTE))
        self.model_status = S.DISPUTED

    @precondition(lambda self: self._in(S.DISPUTED))
    @rule()
    def resolve(self) -> None:
        self.apply(Advance(self.key(), "t", E.RESOLVE_DISPUTE))
        # A dispute does not un-refund anything.
        self.model_status = S.PARTIALLY_REFUNDED if self.model_refunded else S.SETTLED

    @precondition(lambda self: self._in(S.SETTLED, S.PARTIALLY_REFUNDED))
    @rule(data=st.data())
    def refund(self, data: st.DataObject) -> None:
        """Refund some of what is left. Draws from the *remaining* range so most attempts
        are legal and partial, which is the path to the deep states."""
        remaining = self.amount - self.model_refunded
        amount = data.draw(st.integers(min_value=1, max_value=remaining), label="refund")
        self.apply(Refund(self.key(), "t", Money(amount, USD), refund_entry(amount)))
        self.model_refunded += amount
        self.model_cash -= amount
        self.model_status = (
            S.REFUNDED if self.model_refunded == self.amount else S.PARTIALLY_REFUNDED
        )

    @precondition(lambda self: self._in(S.SETTLED, S.PARTIALLY_REFUNDED))
    @rule(excess=st.integers(min_value=1, max_value=5_000))
    def over_refund_is_refused(self, excess: int) -> None:
        remaining = self.amount - self.model_refunded
        amount = remaining + excess
        cmd = Refund(self.key(), "t", Money(amount, USD), refund_entry(amount))
        with pytest.raises(RefundExceedsSettledError):
            self.ledger.execute(cmd, clock=self.clock, ids=self.ids)

    @rule(event=st.sampled_from(list(E)))
    def illegal_event_is_refused(self, event: TransactionEvent) -> None:
        """Any event the model says is illegal here must be rejected by the ledger."""
        if event in allowed_events(self.model_status):
            return
        if event is E.REFUND:
            cmd: Command = Refund(self.key(), "t", Money(1, USD), refund_entry(1))
        else:
            entry = settlement(self.amount) if event is E.SETTLE else None
            cmd = Advance(self.key(), "t", event, entry)
        with pytest.raises(IllegalTransitionError):
            self.ledger.execute(cmd, clock=self.clock, ids=self.ids)

    @precondition(lambda self: self.last_applied is not None)
    @rule()
    def retry_last_command_is_a_noop(self) -> None:
        """A network retry of the last successful command must not apply twice."""
        assert self.last_applied is not None
        before = self.ledger
        result = before.execute(self.last_applied, clock=self.clock, ids=self.ids)
        assert result.replayed
        assert result.ledger is before

    # ----------------------------------------------------------- invariants

    @invariant()
    def status_matches_model(self) -> None:
        assert self.ledger.transaction("t").status is self.model_status

    @invariant()
    def refunded_never_exceeds_amount(self) -> None:
        txn = self.ledger.transaction("t")
        assert 0 <= txn.refunded.amount <= txn.amount.amount
        assert txn.refunded.amount == self.model_refunded

    @invariant()
    def refunded_status_iff_fully_refunded(self) -> None:
        txn = self.ledger.transaction("t")
        if txn.status is S.REFUNDED:
            assert txn.refundable.is_zero
        if txn.refunded.amount and txn.status not in (S.DISPUTED, S.REFUNDED):
            assert txn.status is S.PARTIALLY_REFUNDED
        if txn.status is S.SETTLED:
            assert txn.refunded.is_zero, "SETTLED with money already refunded is a lie"

    @invariant()
    def terminal_states_are_final(self) -> None:
        if self.model_status in TERMINAL:
            assert allowed_events(self.model_status) == frozenset()

    @invariant()
    def cash_matches_model_and_books_balance(self) -> None:
        assert self.ledger.raw_balance("cash") == self.model_cash
        assert self.ledger.trial_balance().is_balanced
        assert self.ledger.verify_chain()


TestPaymentLifecycle = PaymentLifecycle.TestCase
TestPaymentLifecycle.settings = settings(max_examples=150, stateful_step_count=30)
