"""Injected effects, the payment state machine, and FX entry construction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from fractions import Fraction

import pytest

from ledgergate.ledger import (
    EPOCH,
    EUR,
    JPY,
    TERMINAL,
    TRANSITIONS,
    USD,
    Clock,
    CurrencyMismatchError,
    FixedClock,
    FxRateSource,
    IdGenerator,
    IllegalTransitionError,
    InvalidAmountError,
    InvalidIdentifierError,
    MissingRateError,
    Money,
    RefundExceedsSettledError,
    RoundingMode,
    SequentialIds,
    StaticRates,
    SteppingClock,
    Transaction,
    TransactionEvent,
    TransactionStatus,
    allowed_events,
    conversion_entry,
    credit,
    debit,
    price,
    transition,
)

S, E = TransactionStatus, TransactionEvent


class TestEffects:
    def test_reference_impls_satisfy_protocols(self) -> None:
        assert isinstance(FixedClock(EPOCH), Clock)
        assert isinstance(SteppingClock(EPOCH), Clock)
        assert isinstance(SequentialIds(), IdGenerator)
        assert isinstance(StaticRates({}), FxRateSource)

    def test_fixed_clock(self) -> None:
        c = FixedClock(EPOCH)
        assert c.now() == c.now() == EPOCH

    def test_stepping_clock(self) -> None:
        c = SteppingClock(EPOCH, timedelta(minutes=1))
        assert c.now() == EPOCH
        assert c.now() == EPOCH + timedelta(minutes=1)

    @pytest.mark.parametrize("step", [timedelta(0), timedelta(seconds=-1)])
    def test_stepping_clock_must_advance(self, step: timedelta) -> None:
        with pytest.raises(InvalidAmountError):
            SteppingClock(EPOCH, step)

    def test_clocks_require_tz(self) -> None:
        with pytest.raises(InvalidAmountError):
            FixedClock(datetime(2026, 1, 1))  # noqa: DTZ001 -- the naive case is the point
        with pytest.raises(InvalidAmountError):
            SteppingClock(datetime(2026, 1, 1))  # noqa: DTZ001

    def test_sequential_ids(self) -> None:
        ids = SequentialIds("txn", start=7, width=3)
        assert ids.next_id() == "txn-007"
        assert ids.next_id() == "txn-008"
        assert SequentialIds().next_id() == "e-000001"

    def test_static_rates_derive_inverse(self) -> None:
        rates = StaticRates({(USD, EUR): Fraction(9, 10)})
        assert rates.rate(USD, EUR) == Fraction(9, 10)
        assert rates.rate(EUR, USD) == Fraction(10, 9)
        assert rates.rate(USD, USD) == 1
        with pytest.raises(MissingRateError) as exc:
            rates.rate(USD, JPY)
        assert (exc.value.base, exc.value.quote) == ("USD", "JPY")

    def test_static_rates_reject_nonpositive(self) -> None:
        with pytest.raises(InvalidAmountError):
            StaticRates({(USD, EUR): Fraction(0)})

    def test_epoch_is_aware(self) -> None:
        assert EPOCH.tzinfo is UTC


class TestTransitions:
    def test_happy_path(self) -> None:
        assert transition("t", S.PENDING, E.AUTHORIZE) is S.AUTHORIZED
        assert transition("t", S.AUTHORIZED, E.SETTLE) is S.SETTLED
        assert transition("t", S.SETTLED, E.DISPUTE) is S.DISPUTED
        assert transition("t", S.DISPUTED, E.RESOLVE_DISPUTE) is S.SETTLED

    @pytest.mark.parametrize(
        ("status", "event"),
        [
            (S.PENDING, E.SETTLE),
            (S.PENDING, E.REFUND),
            (S.AUTHORIZED, E.REFUND),
            (S.SETTLED, E.AUTHORIZE),
            (S.REFUNDED, E.REFUND),
            (S.CANCELLED, E.AUTHORIZE),
            (S.FAILED, E.SETTLE),
            (S.DISPUTED, E.REFUND),
        ],
    )
    def test_illegal(self, status: TransactionStatus, event: TransactionEvent) -> None:
        with pytest.raises(IllegalTransitionError) as exc:
            transition("t-1", status, event)
        assert exc.value.transaction_id == "t-1"
        assert exc.value.status == status.value and exc.value.event == event.value

    def test_terminal_states_have_no_exits(self) -> None:
        for status in TERMINAL:
            assert allowed_events(status) == frozenset()
            assert status.is_terminal
        assert not S.SETTLED.is_terminal

    def test_allowed_events_matches_table(self) -> None:
        assert allowed_events(S.PENDING) == {E.AUTHORIZE, E.CANCEL, E.FAIL}
        assert all(isinstance(k, tuple) for k in TRANSITIONS)


class TestTransaction:
    def test_defaults(self) -> None:
        t = Transaction("t", Money(1000, USD))
        assert t.status is S.PENDING
        assert t.refunded == Money(0, USD)
        assert t.refundable == Money(1000, USD)

    def test_rejects_nonpositive_amount(self) -> None:
        with pytest.raises(InvalidAmountError):
            Transaction("t", Money(0, USD))

    def test_rejects_inconsistent_refunded(self) -> None:
        with pytest.raises(InvalidAmountError):
            Transaction("t", Money(10, USD), refunded_minor=11)
        with pytest.raises(InvalidAmountError):
            Transaction("t", Money(10, USD), refunded_minor=True)

    @pytest.mark.parametrize(
        ("status", "refunded"),
        [
            (S.REFUNDED, 0),  # "fully refunded" with everything still refundable
            (S.REFUNDED, 5),
            (S.PARTIALLY_REFUNDED, 0),
            (S.PARTIALLY_REFUNDED, 10),
            (S.SETTLED, 3),  # money gone but status says none has
            (S.PENDING, 1),
            (S.AUTHORIZED, 1),
            (S.CANCELLED, 1),
            (S.FAILED, 1),
            (S.DISPUTED, 10),
        ],
    )
    def test_status_must_agree_with_refunded_total(
        self, status: TransactionStatus, refunded: int
    ) -> None:
        """The public constructor cannot build a state the machine could never reach."""
        with pytest.raises(InvalidAmountError, match="inconsistent"):
            Transaction("t", Money(10, USD), status=status, refunded_minor=refunded)

    @pytest.mark.parametrize(
        ("status", "refunded"),
        [
            (S.REFUNDED, 10),
            (S.PARTIALLY_REFUNDED, 4),
            (S.SETTLED, 0),
            (S.DISPUTED, 0),
            (S.DISPUTED, 4),
            (S.PENDING, 0),
        ],
    )
    def test_reachable_states_construct(self, status: TransactionStatus, refunded: int) -> None:
        t = Transaction("t", Money(10, USD), status=status, refunded_minor=refunded)
        assert t.refunded.amount == refunded

    def test_advance(self) -> None:
        t = Transaction("t", Money(1000, USD)).advance(E.AUTHORIZE).advance(E.SETTLE)
        assert t.status is S.SETTLED

    def test_advance_refuses_refund_event(self) -> None:
        t = Transaction("t", Money(1000, USD)).advance(E.AUTHORIZE).advance(E.SETTLE)
        with pytest.raises(IllegalTransitionError):
            t.advance(E.REFUND)

    def test_partial_then_full_refund(self) -> None:
        t = Transaction("t", Money(1000, USD)).advance(E.AUTHORIZE).advance(E.SETTLE)
        t = t.refund(Money(300, USD))
        assert t.status is S.PARTIALLY_REFUNDED and t.refundable == Money(700, USD)
        t = t.refund(Money(700, USD))
        assert t.status is S.REFUNDED and t.refundable.is_zero
        with pytest.raises(IllegalTransitionError):
            t.refund(Money(1, USD))

    def test_refund_before_settlement_is_illegal(self) -> None:
        with pytest.raises(IllegalTransitionError):
            Transaction("t", Money(1000, USD)).refund(Money(1, USD))

    def test_over_refund(self) -> None:
        t = Transaction("t", Money(1000, USD)).advance(E.AUTHORIZE).advance(E.SETTLE)
        with pytest.raises(RefundExceedsSettledError) as exc:
            t.refund(Money(1001, USD))
        assert (exc.value.remaining, exc.value.attempted) == (1000, 1001)
        with pytest.raises(RefundExceedsSettledError):
            t.refund(Money(0, USD))

    def test_resolving_a_dispute_remembers_partial_refunds(self) -> None:
        """SETTLED -> refund -> DISPUTED -> resolve must land in PARTIALLY_REFUNDED."""
        t = Transaction("t", Money(1000, USD)).advance(E.AUTHORIZE).advance(E.SETTLE)
        t = t.refund(Money(1, USD)).advance(E.DISPUTE).advance(E.RESOLVE_DISPUTE)
        assert t.status is S.PARTIALLY_REFUNDED
        assert t.refunded == Money(1, USD)
        clean = Transaction("t", Money(1000, USD)).advance(E.AUTHORIZE).advance(E.SETTLE)
        assert clean.advance(E.DISPUTE).advance(E.RESOLVE_DISPUTE).status is S.SETTLED

    def test_transaction_id_must_be_usable(self) -> None:
        for bad in ("", " t", "t\n"):
            with pytest.raises(InvalidIdentifierError):
                Transaction(bad, Money(1, USD))

    def test_refund_wrong_currency(self) -> None:
        t = Transaction("t", Money(1000, USD)).advance(E.AUTHORIZE).advance(E.SETTLE)
        with pytest.raises(CurrencyMismatchError):
            t.refund(Money(1, EUR))


class TestFx:
    rates = StaticRates({(USD, EUR): Fraction(9, 10), (USD, JPY): Fraction(150)})

    def test_price(self) -> None:
        c = price(Money(1000, USD), EUR, self.rates)
        assert c.destination == Money(900, EUR)
        assert c.rate == "9/10"

    def test_conversion_entry_balances_per_currency(self) -> None:
        d = conversion_entry(
            Money(1000, USD),
            EUR,
            self.rates,
            source_account="cash",
            destination_account="cash:eur",
            source_clearing="fx:usd",
            destination_clearing="fx:eur",
        )
        assert d.postings == (
            credit("cash", Money(1000, USD)),
            debit("fx:usd", Money(1000, USD)),
            credit("fx:eur", Money(900, EUR)),
            debit("cash:eur", Money(900, EUR)),
        )
        assert d.tag("fx_rate") == "9/10"
        assert "10.00 USD" in d.description and "9.00 EUR" in d.description

    def test_same_currency_never_consults_the_rate_source(self) -> None:
        class Broken:
            def rate(self, base: object, quote: object) -> Fraction:
                return Fraction(2)

        c = price(Money(100, USD), USD, Broken())
        assert c.destination == Money(100, USD) and c.rate == "1/1"

    def test_same_currency_is_plain_transfer(self) -> None:
        d = conversion_entry(
            Money(500, USD),
            USD,
            self.rates,
            source_account="cash",
            destination_account="fx:usd",
            source_clearing="unused",
            destination_clearing="unused",
            description="move",
        )
        assert len(d.postings) == 2 and d.description == "move"

    def test_rounding_mode_is_honoured(self) -> None:
        rates = StaticRates({(USD, EUR): Fraction(1, 3)})
        # 0.05 USD * 1/3 = 0.01666.. EUR -> 2 minor units HALF_EVEN, 1 with FLOOR
        assert price(Money(5, USD), EUR, rates).destination == Money(2, EUR)
        assert price(Money(5, USD), EUR, rates, RoundingMode.FLOOR).destination == Money(1, EUR)

    def test_rejects_nonpositive_and_vanishing_amounts(self) -> None:
        def convert(money: Money, rates: StaticRates) -> None:
            conversion_entry(
                money,
                EUR,
                rates,
                source_account="a",
                destination_account="b",
                source_clearing="c",
                destination_clearing="d",
            )

        with pytest.raises(InvalidAmountError):
            convert(Money(0, USD), self.rates)
        tiny = StaticRates({(USD, EUR): Fraction(1, 1000)})
        with pytest.raises(InvalidAmountError, match="below one minor unit"):
            convert(Money(1, USD), tiny)
