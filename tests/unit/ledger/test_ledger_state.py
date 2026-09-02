"""The Ledger: posting, idempotency, reversal, hash chain, atomic lifecycle operations."""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType
from typing import Any

import pytest

from ledgergate.ledger import (
    EPOCH,
    EUR,
    GENESIS_HASH,
    USD,
    AccountCurrencyMismatchError,
    Advance,
    AlreadyReversedError,
    ChainIntegrityError,
    ChartOfAccounts,
    Command,
    DuplicateEntryIdError,
    DuplicateTransactionError,
    EntryAmountMismatchError,
    EntryDraft,
    EntryNotAllowedError,
    EntryRequiredError,
    FixedClock,
    IdempotencyConflictError,
    IllegalTransitionError,
    InsufficientFundsError,
    InvalidIdentifierError,
    Ledger,
    Money,
    OpenTransaction,
    Post,
    Refund,
    Reverse,
    SequentialIds,
    SteppingClock,
    TransactionEvent,
    TransactionStatus,
    UnknownAccountError,
    UnknownEntryError,
    UnknownTransactionError,
    credit,
    debit,
    replay,
)
from ledgergate.ledger.entries import encode

E, S = TransactionEvent, TransactionStatus


def sale(amount: int = 1000) -> EntryDraft:
    return EntryDraft.of(
        debit("cash", Money(amount, USD)), credit("revenue", Money(amount, USD)), description="sale"
    )


def topup(amount: int = 500) -> EntryDraft:
    return EntryDraft.of(
        debit("cash", Money(amount, USD)), credit("wallet:alice", Money(amount, USD))
    )


class TestPosting:
    def test_empty_ledger(self, ledger: Ledger) -> None:
        assert ledger.sequence == 0
        assert ledger.head == GENESIS_HASH
        assert ledger.balance("cash").is_zero
        assert ledger.trial_balance().is_balanced
        assert ledger.verify_chain()

    def test_post_updates_balances_and_chain(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        result = ledger.post(sale(1000), key="k1", clock=clock, ids=ids)
        new = result.ledger
        assert result.entry is not None and result.entry.entry_id == "e-000001"
        assert result.entry.sequence == 1 and result.entry.posted_at == EPOCH
        assert result.entry.previous_hash == GENESIS_HASH
        assert new.head == result.entry.digest != GENESIS_HASH
        assert new.balance("cash") == Money(1000, USD)
        assert new.balance("revenue") == Money(1000, USD)
        assert new.raw_balance("revenue") == -1000
        assert new.sequence == 1
        assert new.entry("e-000001") is result.entry
        assert new.entries_for("cash") == (result.entry,)
        assert new.entries_for("fees") == ()
        assert new.verify_chain()

    def test_original_ledger_is_untouched(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        ledger.post(sale(), key="k1", clock=clock, ids=ids)
        assert ledger.sequence == 0 and ledger.balance("cash").is_zero

    def test_unknown_account(self, ledger: Ledger, clock: FixedClock, ids: SequentialIds) -> None:
        draft = EntryDraft.of(debit("nope", Money(1, USD)), credit("revenue", Money(1, USD)))
        with pytest.raises(UnknownAccountError):
            ledger.post(draft, key="k", clock=clock, ids=ids)
        with pytest.raises(UnknownAccountError):
            ledger.balance("nope")
        with pytest.raises(UnknownAccountError):
            ledger.entries_for("nope")

    def test_account_currency_mismatch(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        draft = EntryDraft.of(debit("cash", Money(1, EUR)), credit("cash:eur", Money(1, EUR)))
        with pytest.raises(AccountCurrencyMismatchError) as exc:
            ledger.post(draft, key="k", clock=clock, ids=ids)
        assert (exc.value.account_id, exc.value.expected, exc.value.actual) == (
            "cash",
            "USD",
            "EUR",
        )

    def test_effects_not_consumed_on_failure(self, ledger: Ledger, ids: SequentialIds) -> None:
        clock = SteppingClock(EPOCH)
        bad = EntryDraft.of(debit("nope", Money(1, USD)), credit("revenue", Money(1, USD)))
        with pytest.raises(UnknownAccountError):
            ledger.post(bad, key="k", clock=clock, ids=ids)
        ok = ledger.post(sale(), key="k2", clock=clock, ids=ids).entry
        assert ok is not None and ok.entry_id == "e-000001" and ok.posted_at == EPOCH

    def test_unknown_entry(self, ledger: Ledger) -> None:
        with pytest.raises(UnknownEntryError):
            ledger.entry("e-999")
        with pytest.raises(UnknownEntryError):
            ledger.reversal_of("e-999")


class TestNegativeBalances:
    def test_wallet_cannot_be_overdrawn(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        funded = ledger.post(topup(500), key="k1", clock=clock, ids=ids).ledger
        assert funded.balance("wallet:alice") == Money(500, USD)
        payout = EntryDraft.of(
            debit("wallet:alice", Money(600, USD)), credit("cash", Money(600, USD))
        )
        with pytest.raises(InsufficientFundsError) as exc:
            funded.post(payout, key="k2", clock=clock, ids=ids)
        assert exc.value.account_id == "wallet:alice"
        assert (exc.value.balance, exc.value.attempted) == (500, -600)

    def test_exactly_to_zero_is_fine(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        funded = ledger.post(topup(500), key="k1", clock=clock, ids=ids).ledger
        payout = EntryDraft.of(
            debit("wallet:alice", Money(500, USD)), credit("cash", Money(500, USD))
        )
        drained = funded.post(payout, key="k2", clock=clock, ids=ids).ledger
        assert drained.balance("wallet:alice").is_zero

    def test_asset_may_go_negative_when_allowed(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        spend = EntryDraft.of(debit("fees", Money(50, USD)), credit("cash", Money(50, USD)))
        new = ledger.post(spend, key="k", clock=clock, ids=ids).ledger
        assert new.balance("cash") == Money(-50, USD)


class TestIdempotency:
    def test_replay_returns_original_without_reapplying(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        first = ledger.post(sale(), key="refund-42", clock=clock, ids=ids)
        again = first.ledger.post(sale(), key="refund-42", clock=clock, ids=ids)
        assert again.replayed and not first.replayed
        assert again.entry is first.entry
        assert again.ledger is first.ledger
        assert again.ledger.sequence == 1
        assert again.ledger.balance("cash") == Money(1000, USD)

    def test_same_key_different_request_conflicts(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        first = ledger.post(sale(1000), key="k", clock=clock, ids=ids).ledger
        with pytest.raises(IdempotencyConflictError) as exc:
            first.post(sale(1001), key="k", clock=clock, ids=ids)
        assert exc.value.key == "k"

    def test_different_keys_same_request_both_apply(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        one = ledger.post(sale(), key="a", clock=clock, ids=ids).ledger
        two = one.post(sale(), key="b", clock=clock, ids=ids).ledger
        assert two.sequence == 2 and two.balance("cash") == Money(2000, USD)

    def test_key_is_shared_across_command_kinds(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        one = ledger.post(sale(), key="k", clock=clock, ids=ids).ledger
        with pytest.raises(IdempotencyConflictError):
            one.execute(OpenTransaction("k", "t", Money(1, USD)), clock=clock, ids=ids)

    def test_replayed_transaction_op_returns_transaction(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        cmd = OpenTransaction("k", "t", Money(100, USD))
        first = ledger.execute(cmd, clock=clock, ids=ids)
        again = first.ledger.execute(cmd, clock=clock, ids=ids)
        assert again.replayed and again.transaction == first.transaction and again.entry is None


class TestReversal:
    def test_reverse_posts_mirror_and_links(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        posted = ledger.post(sale(), key="k1", clock=clock, ids=ids)
        assert posted.entry is not None
        reversed_ = posted.ledger.execute(
            Reverse("k2", posted.entry.entry_id, "oops"), clock=clock, ids=ids
        )
        assert reversed_.entry is not None
        assert reversed_.entry.reverses == posted.entry.entry_id
        assert reversed_.entry.description == "oops"
        assert reversed_.ledger.balance("cash").is_zero
        assert reversed_.ledger.balance("revenue").is_zero
        assert reversed_.ledger.sequence == 2, "append-only: nothing was deleted"
        assert reversed_.ledger.reversal_of(posted.entry.entry_id) is reversed_.entry
        assert reversed_.ledger.reversal_of(reversed_.entry.entry_id) is None

    def test_cannot_reverse_twice(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        posted = ledger.post(sale(), key="k1", clock=clock, ids=ids)
        assert posted.entry is not None
        once = posted.ledger.execute(Reverse("k2", posted.entry.entry_id), clock=clock, ids=ids)
        with pytest.raises(AlreadyReversedError) as exc:
            once.ledger.execute(Reverse("k3", posted.entry.entry_id), clock=clock, ids=ids)
        assert exc.value.reversed_by == "e-000002"

    def test_reverse_is_idempotent(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        posted = ledger.post(sale(), key="k1", clock=clock, ids=ids)
        assert posted.entry is not None
        cmd = Reverse("k2", posted.entry.entry_id)
        once = posted.ledger.execute(cmd, clock=clock, ids=ids)
        twice = once.ledger.execute(cmd, clock=clock, ids=ids)
        assert twice.replayed and twice.ledger.sequence == 2

    def test_reverse_unknown(self, ledger: Ledger, clock: FixedClock, ids: SequentialIds) -> None:
        with pytest.raises(UnknownEntryError):
            ledger.execute(Reverse("k", "e-404"), clock=clock, ids=ids)

    def test_reversal_respects_negative_balance_rule(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        funded = ledger.post(topup(500), key="k1", clock=clock, ids=ids)
        assert funded.entry is not None
        payout = EntryDraft.of(
            debit("wallet:alice", Money(500, USD)), credit("cash", Money(500, USD))
        )
        drained = funded.ledger.post(payout, key="k2", clock=clock, ids=ids).ledger
        # Reversing the top-up would push the wallet to -500: refused.
        with pytest.raises(InsufficientFundsError):
            drained.execute(Reverse("k3", funded.entry.entry_id), clock=clock, ids=ids)


class TestChain:
    def test_chain_links_and_verifies(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        a = ledger.post(sale(1), key="a", clock=clock, ids=ids)
        b = a.ledger.post(sale(2), key="b", clock=clock, ids=ids)
        assert a.entry is not None and b.entry is not None
        assert b.entry.previous_hash == a.entry.digest
        assert b.ledger.head == b.entry.digest
        assert b.ledger.verify_chain()

    def test_tampered_entry_is_detected(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        good = ledger.post(sale(), key="a", clock=clock, ids=ids).ledger
        forged = replace(good.entries[0], draft=sale(999))
        tampered = replace(good, entries=(forged,))
        with pytest.raises(ChainIntegrityError) as exc:
            tampered.verify_chain()
        assert exc.value.sequence == 1

    def test_broken_link_is_detected(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        two = ledger.post(sale(1), key="a", clock=clock, ids=ids).ledger
        two = two.post(sale(2), key="b", clock=clock, ids=ids).ledger
        dropped = replace(two, entries=(two.entries[1],))
        with pytest.raises(ChainIntegrityError):
            dropped.verify_chain()

    def test_wrong_head_is_detected(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        one = ledger.post(sale(), key="a", clock=clock, ids=ids).ledger
        with pytest.raises(ChainIntegrityError):
            replace(one, head=GENESIS_HASH).verify_chain()

    def test_forged_balance_is_detected(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        """Editing a balance without touching the entries must not pass as intact."""
        one = ledger.post(sale(1000), key="a", clock=clock, ids=ids).ledger
        forged = replace(one, _balances={**one._balances, "cash": 999_999})
        assert forged.balance("cash") == Money(999_999, USD), "the lie is visible..."
        with pytest.raises(ChainIntegrityError, match=r"expected .balances."):
            forged.verify_chain()

    def test_forged_balance_on_empty_ledger_is_detected(self, ledger: Ledger) -> None:
        with pytest.raises(ChainIntegrityError):
            replace(ledger, _balances={"cash": 1}).verify_chain()

    def test_forged_index_is_detected(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        one = ledger.post(sale(), key="a", clock=clock, ids=ids).ledger
        with pytest.raises(ChainIntegrityError, match="entry index"):
            replace(one, _by_id={}).verify_chain()
        with pytest.raises(ChainIntegrityError, match="reversal index"):
            replace(one, _reversals={"e-000001": "e-000009"}).verify_chain()

    def test_duplicate_entry_id_from_bad_generator_is_refused(
        self, ledger: Ledger, clock: FixedClock
    ) -> None:
        """The IdGenerator promises fresh ids; the ledger checks anyway."""
        one = ledger.post(sale(1), key="a", clock=clock, ids=SequentialIds()).ledger
        with pytest.raises(DuplicateEntryIdError) as exc:
            one.post(sale(2), key="b", clock=clock, ids=SequentialIds())  # restarts at e-000001
        assert exc.value.entry_id == "e-000001"
        assert one.sequence == 1

    def test_duplicate_id_smuggled_into_entries_is_detected(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        one = ledger.post(sale(1), key="a", clock=clock, ids=ids).ledger
        two = one.post(sale(2), key="b", clock=clock, ids=ids).ledger
        second = two.entries[1]
        clone = replace(second, entry_id="e-000001")
        clone = replace(clone, digest=clone.recomputed_digest())
        smuggled = replace(two, entries=(two.entries[0], clone), head=clone.digest)
        with pytest.raises(ChainIntegrityError, match="unique id"):
            smuggled.verify_chain()


class TestEquality:
    def test_replayed_ledgers_are_equal(self, chart: ChartOfAccounts) -> None:
        commands: list[Command] = [Post("a", sale()), Post("b", topup())]
        one = replay(chart, commands, clock=FixedClock(EPOCH), ids=SequentialIds())
        two = replay(chart, commands, clock=FixedClock(EPOCH), ids=SequentialIds())
        assert one == two and hash(one) == hash(two)
        assert one is not two

    def test_different_history_is_unequal(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        one = ledger.post(sale(), key="a", clock=clock, ids=ids).ledger
        assert one != ledger
        assert ledger != "not a ledger"

    def test_repr_hides_derived_indexes(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        one = ledger.post(sale(), key="a", clock=clock, ids=ids).ledger
        assert "_balances" not in repr(one) and "_by_id" not in repr(one)


class TestTrialBalance:
    def test_rows_and_totals(self, ledger: Ledger, clock: FixedClock, ids: SequentialIds) -> None:
        new = ledger.post(sale(1000), key="a", clock=clock, ids=ids).ledger
        spend = EntryDraft.of(debit("fees", Money(50, USD)), credit("cash", Money(50, USD)))
        new = new.post(spend, key="b", clock=clock, ids=ids).ledger
        tb = new.trial_balance()
        by_id = {r.account.account_id: r for r in tb.rows}
        assert by_id["cash"].debit == Money(950, USD) and by_id["cash"].credit.is_zero
        assert by_id["revenue"].credit == Money(1000, USD)
        assert by_id["revenue"].balance == Money(1000, USD), "credit-normal reads positive"
        assert by_id["fees"].balance == Money(50, USD)
        debit_total, credit_total = tb.totals()[USD]
        assert debit_total == credit_total == Money(1000, USD)
        assert tb.is_balanced
        assert tb.totals()[EUR] == (Money.zero(EUR), Money.zero(EUR))


class TestLifecycle:
    def settle(self, ledger: Ledger, clock: FixedClock, ids: SequentialIds) -> Ledger:
        led = ledger.execute(OpenTransaction("o", "t1", Money(1000, USD)), clock=clock, ids=ids)
        led = led.ledger.execute(Advance("a", "t1", E.AUTHORIZE), clock=clock, ids=ids)
        return led.ledger.execute(
            Advance("s", "t1", E.SETTLE, sale(1000)), clock=clock, ids=ids
        ).ledger

    def test_open_and_advance(self, ledger: Ledger, clock: FixedClock, ids: SequentialIds) -> None:
        settled = self.settle(ledger, clock, ids)
        txn = settled.transaction("t1")
        assert txn.status is S.SETTLED
        assert settled.sequence == 1, "authorize posted nothing, settle posted one entry"
        assert settled.balance("cash") == Money(1000, USD)

    def test_open_duplicate(self, ledger: Ledger, clock: FixedClock, ids: SequentialIds) -> None:
        one = ledger.execute(OpenTransaction("o", "t1", Money(1, USD)), clock=clock, ids=ids).ledger
        with pytest.raises(DuplicateTransactionError):
            one.execute(OpenTransaction("o2", "t1", Money(1, USD)), clock=clock, ids=ids)

    def test_unknown_transaction(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        with pytest.raises(UnknownTransactionError):
            ledger.execute(Advance("a", "nope", E.AUTHORIZE), clock=clock, ids=ids)
        with pytest.raises(UnknownTransactionError):
            ledger.transaction("nope")

    def test_illegal_transition_posts_nothing(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        opened = ledger.execute(OpenTransaction("o", "t1", Money(1000, USD)), clock=clock, ids=ids)
        with pytest.raises(IllegalTransitionError):
            opened.ledger.execute(Refund("r", "t1", Money(1, USD), sale(1)), clock=clock, ids=ids)
        with pytest.raises(IllegalTransitionError):
            opened.ledger.execute(Advance("s", "t1", E.SETTLE, sale(1000)), clock=clock, ids=ids)
        assert opened.ledger.sequence == 0
        assert opened.ledger.transaction("t1").status is S.PENDING

    def test_refund_transitions_and_posts_atomically(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        settled = self.settle(ledger, clock, ids)
        refund_entry = EntryDraft.of(
            debit("revenue", Money(300, USD)), credit("cash", Money(300, USD))
        )
        result = settled.execute(
            Refund("r1", "t1", Money(300, USD), refund_entry), clock=clock, ids=ids
        )
        assert result.transaction is not None and result.transaction.status is S.PARTIALLY_REFUNDED
        assert result.entry is not None and result.entry.sequence == 2
        assert result.ledger.balance("cash") == Money(700, USD)

    def test_refund_entry_failure_leaves_transaction_unchanged(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        settled = self.settle(ledger, clock, ids)
        bad_entry = EntryDraft.of(debit("nope", Money(300, USD)), credit("cash", Money(300, USD)))
        with pytest.raises(UnknownAccountError):
            settled.execute(Refund("r1", "t1", Money(300, USD), bad_entry), clock=clock, ids=ids)
        assert settled.transaction("t1").status is S.SETTLED
        assert settled.transaction("t1").refunded.is_zero

    def test_double_refund_is_replayed_not_reapplied(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        """The README problem statement: a retry without idempotency pays out twice."""
        settled = self.settle(ledger, clock, ids)
        refund_entry = EntryDraft.of(
            debit("revenue", Money(1000, USD)), credit("cash", Money(1000, USD))
        )
        cmd = Refund("refund-t1", "t1", Money(1000, USD), refund_entry)
        first = settled.execute(cmd, clock=clock, ids=ids)
        retry = first.ledger.execute(cmd, clock=clock, ids=ids)
        assert retry.replayed
        assert retry.ledger.balance("cash").is_zero, "paid out once, not twice"
        assert retry.ledger.transaction("t1").status is S.REFUNDED
        assert retry.ledger.sequence == 2

    def test_refund_without_entry_is_refused(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        """A refund moves money; a refund with no entry is a lifecycle lie."""
        settled = self.settle(ledger, clock, ids)
        with pytest.raises(EntryRequiredError):
            settled.execute(Refund("r", "t1", Money(1000, USD)), clock=clock, ids=ids)
        assert settled.transaction("t1").status is S.SETTLED

    def test_settle_without_entry_is_refused(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        led = ledger.execute(OpenTransaction("o", "t1", Money(1000, USD)), clock=clock, ids=ids)
        led = led.ledger.execute(Advance("a", "t1", E.AUTHORIZE), clock=clock, ids=ids)
        with pytest.raises(EntryRequiredError):
            led.ledger.execute(Advance("s", "t1", E.SETTLE), clock=clock, ids=ids)

    def test_entry_must_move_the_transaction_amount(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        """Settling 100.00 with a 0.01 entry is not a settlement."""
        led = ledger.execute(OpenTransaction("o", "t1", Money(10_000, USD)), clock=clock, ids=ids)
        led = led.ledger.execute(Advance("a", "t1", E.AUTHORIZE), clock=clock, ids=ids)
        with pytest.raises(EntryAmountMismatchError) as exc:
            led.ledger.execute(Advance("s", "t1", E.SETTLE, sale(1)), clock=clock, ids=ids)
        assert (exc.value.expected, exc.value.actual) == ("100.00 USD", "0.01 USD")
        assert led.ledger.sequence == 0

    def test_refund_entry_must_move_the_refund_amount(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        settled = self.settle(ledger, clock, ids)
        one_cent = EntryDraft.of(debit("revenue", Money(1, USD)), credit("cash", Money(1, USD)))
        with pytest.raises(EntryAmountMismatchError):
            settled.execute(Refund("r", "t1", Money(300, USD), one_cent), clock=clock, ids=ids)
        unrelated = EntryDraft.of(debit("fees", Money(300, USD)), credit("cash", Money(300, USD)))
        # Moves the right amount in the right currency; which accounts is the invariants
        # layer's problem, so this is accepted here.
        ok = settled.execute(Refund("r", "t1", Money(300, USD), unrelated), clock=clock, ids=ids)
        assert ok.transaction is not None and ok.transaction.refunded == Money(300, USD)

    def test_entry_in_wrong_currency_does_not_count(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        led = ledger.execute(OpenTransaction("o", "t1", Money(100, USD)), clock=clock, ids=ids)
        led = led.ledger.execute(Advance("a", "t1", E.AUTHORIZE), clock=clock, ids=ids)
        eur = EntryDraft.of(debit("cash:eur", Money(100, EUR)), credit("fx:eur", Money(100, EUR)))
        with pytest.raises(EntryAmountMismatchError) as exc:
            led.ledger.execute(Advance("s", "t1", E.SETTLE, eur), clock=clock, ids=ids)
        assert exc.value.actual == "0.00 USD"

    def test_non_monetary_event_must_not_carry_an_entry(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        opened = ledger.execute(OpenTransaction("o", "t1", Money(1000, USD)), clock=clock, ids=ids)
        with pytest.raises(EntryNotAllowedError):
            opened.ledger.execute(Advance("a", "t1", E.AUTHORIZE, sale(1000)), clock=clock, ids=ids)

    def test_resolved_dispute_keeps_partial_refund(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        settled = self.settle(ledger, clock, ids)
        one = EntryDraft.of(debit("revenue", Money(1, USD)), credit("cash", Money(1, USD)))
        led = settled.execute(Refund("r", "t1", Money(1, USD), one), clock=clock, ids=ids).ledger
        led = led.execute(Advance("d", "t1", E.DISPUTE), clock=clock, ids=ids).ledger
        led = led.execute(Advance("x", "t1", E.RESOLVE_DISPUTE), clock=clock, ids=ids).ledger
        assert led.transaction("t1").status is S.PARTIALLY_REFUNDED


class TestImmutability:
    def test_mappings_cannot_be_mutated(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        """`ledger.operations.clear()` must be an error, not a way to disable idempotency."""
        one = ledger.post(sale(), key="k", clock=clock, ids=ids).ledger
        views: list[Any] = [
            one.operations,
            one.transactions,
            one._balances,
            one._by_id,
            one._reversals,
            one.chart._accounts,
        ]
        for view in views:
            with pytest.raises((TypeError, AttributeError)):
                view.clear()
            with pytest.raises(TypeError):
                view["x"] = None
            with pytest.raises(TypeError):
                del view["k"]

    def test_mutating_the_source_dict_after_construction_has_no_effect(
        self, chart: ChartOfAccounts
    ) -> None:
        source: dict[str, object] = {"k": "record"}
        led = Ledger(chart, operations=source)  # type: ignore[arg-type]
        source.clear()
        assert "k" in led.operations

    def test_incoming_proxy_is_copied_not_trusted(self, chart: ChartOfAccounts) -> None:
        """A MappingProxyType is only a view; its owner can still write through it."""
        backing: dict[str, object] = {}
        led = Ledger(chart, operations=MappingProxyType(backing))  # type: ignore[arg-type]
        backing["injected"] = None
        assert "injected" not in led.operations

    def test_retry_after_attempted_tamper_is_still_a_replay(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        one = ledger.post(sale(), key="k", clock=clock, ids=ids).ledger
        ops: Any = one.operations
        with pytest.raises((TypeError, AttributeError)):
            ops.clear()
        again = one.post(sale(), key="k", clock=clock, ids=ids)
        assert again.replayed and again.ledger.sequence == 1


class TestCanonicalization:
    def test_tag_delimiter_collision_is_a_conflict_not_a_replay(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        """Two different drafts must never share a fingerprint."""
        lines = (debit("cash", Money(1, USD)), credit("revenue", Money(1, USD)))
        a = EntryDraft(lines, tags=(("x", "1;y=2"),))
        b = EntryDraft(lines, tags=(("x", "1"), ("y", "2")))
        assert a != b and a.canonical() != b.canonical()
        one = ledger.post(a, key="k", clock=clock, ids=ids).ledger
        with pytest.raises(IdempotencyConflictError):
            one.post(b, key="k", clock=clock, ids=ids)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (("a", "b"), ("ab", "")),
            (("a:b", "c"), ("a", "b:c")),
            (("1:x", ""), ("", "1:x")),
        ],
    )
    def test_encode_is_injective_on_field_boundaries(
        self, left: tuple[str, ...], right: tuple[str, ...]
    ) -> None:
        assert encode(*left) != encode(*right)

    def test_description_cannot_impersonate_a_posting(self) -> None:
        lines = (debit("cash", Money(1, USD)), credit("revenue", Money(1, USD)))
        sneaky = EntryDraft(lines, description=lines[0].canonical())
        plain = EntryDraft(lines)
        assert sneaky.canonical() != plain.canonical()


class TestIdentifiers:
    @pytest.mark.parametrize("bad", ["", " k", "k ", "k\nk", "\t"])
    def test_bad_idempotency_key_is_refused(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds, bad: str
    ) -> None:
        with pytest.raises(InvalidIdentifierError) as exc:
            ledger.post(sale(), key=bad, clock=clock, ids=ids)
        assert exc.value.what == "idempotency key"
        assert not ledger.operations

    def test_bad_transaction_id_is_refused(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        with pytest.raises(InvalidIdentifierError):
            ledger.execute(OpenTransaction("k", "", Money(1, USD)), clock=clock, ids=ids)
        assert not ledger.transactions

    def test_bad_generated_id_is_refused(self, ledger: Ledger, clock: FixedClock) -> None:
        class Blank:
            def next_id(self) -> str:
                return ""

        with pytest.raises(InvalidIdentifierError) as exc:
            ledger.post(sale(), key="k", clock=clock, ids=Blank())
        assert exc.value.what == "generated entry id"

    def test_cancel_from_pending(
        self, ledger: Ledger, clock: FixedClock, ids: SequentialIds
    ) -> None:
        opened = ledger.execute(
            OpenTransaction("o", "t1", Money(1, USD)), clock=clock, ids=ids
        ).ledger
        done = opened.execute(Advance("c", "t1", E.CANCEL), clock=clock, ids=ids)
        assert done.transaction is not None and done.transaction.status is S.CANCELLED


class TestReplay:
    def test_replay_is_hash_identical(self, chart: ChartOfAccounts) -> None:
        refund_entry = EntryDraft.of(
            debit("revenue", Money(250, USD)), credit("cash", Money(250, USD))
        )
        commands: list[Command] = [
            OpenTransaction("o", "t1", Money(1000, USD)),
            Advance("a", "t1", E.AUTHORIZE),
            Advance("s", "t1", E.SETTLE, sale(1000)),
            Refund("r", "t1", Money(250, USD), refund_entry),
            Refund("r", "t1", Money(250, USD), refund_entry),  # retry
            Post("p", topup(500)),
            Reverse("v", "e-000002"),
        ]
        one = replay(chart, commands, clock=SteppingClock(EPOCH), ids=SequentialIds())
        two = replay(chart, commands, clock=SteppingClock(EPOCH), ids=SequentialIds())
        assert one.head == two.head
        assert [e.digest for e in one.entries] == [e.digest for e in two.entries]
        assert one.trial_balance() == two.trial_balance()
        assert one.verify_chain() and two.verify_chain()
        assert one.sequence == 4

    def test_different_effects_different_ledger(self, chart: ChartOfAccounts) -> None:
        commands = [Post("p", sale())]
        one = replay(chart, commands, clock=FixedClock(EPOCH), ids=SequentialIds("a"))
        two = replay(chart, commands, clock=FixedClock(EPOCH), ids=SequentialIds("b"))
        assert one.head != two.head, "identifiers are part of the audit trail"
