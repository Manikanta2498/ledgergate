"""Money: integer minor units, exact rounding, allocation that sums, no floats."""

from __future__ import annotations

from fractions import Fraction

import pytest

from ledgergate.ledger import (
    EUR,
    JPY,
    KWD,
    USD,
    Currency,
    CurrencyMismatchError,
    InvalidAmountError,
    Money,
    RoundingMode,
    currency,
    round_fraction,
    total,
)


class TestCurrency:
    def test_unit_follows_exponent(self) -> None:
        assert USD.unit == 100
        assert JPY.unit == 1
        assert KWD.unit == 1000

    @pytest.mark.parametrize("code", ["usd", "US", "USDD", "U$D", ""])
    def test_rejects_bad_code(self, code: str) -> None:
        with pytest.raises(InvalidAmountError):
            Currency(code, 2)

    @pytest.mark.parametrize("exponent", [-1, 7])
    def test_rejects_bad_exponent(self, exponent: int) -> None:
        with pytest.raises(InvalidAmountError):
            Currency("USD", exponent)

    def test_lookup(self) -> None:
        assert currency("EUR") is EUR
        with pytest.raises(InvalidAmountError):
            currency("XXX")

    def test_str(self) -> None:
        assert str(USD) == "USD"


class TestMoneyConstruction:
    def test_rejects_float(self) -> None:
        with pytest.raises(InvalidAmountError, match="never a float"):
            Money(19.99, USD)  # type: ignore[arg-type]

    def test_rejects_bool(self) -> None:
        with pytest.raises(InvalidAmountError):
            Money(True, USD)  # bool passes the type checker; the runtime guard must catch it

    def test_rejects_str(self) -> None:
        with pytest.raises(InvalidAmountError):
            Money("100", USD)  # type: ignore[arg-type]

    def test_zero(self) -> None:
        assert Money.zero(USD) == Money(0, USD)
        assert Money.zero(USD).is_zero

    @pytest.mark.parametrize(
        ("text", "cur", "minor"),
        [
            ("19.99", USD, 1999),
            ("19.9", USD, 1990),
            ("19", USD, 1900),
            ("19.", USD, 1900),
            ("-0.01", USD, -1),
            ("+5.00", USD, 500),
            ("500", JPY, 500),
            ("1.234", KWD, 1234),
            ("  3.50 ", USD, 350),
        ],
    )
    def test_parse(self, text: str, cur: Currency, minor: int) -> None:
        assert Money.parse(text, cur) == Money(minor, cur)

    @pytest.mark.parametrize("text", ["19.999", "abc", "1e3", "1,000.00", "", "500.5"])
    def test_parse_rejects(self, text: str) -> None:
        cur = JPY if text == "500.5" else USD
        with pytest.raises(InvalidAmountError):
            Money.parse(text, cur)


class TestMoneyFormatting:
    @pytest.mark.parametrize(
        ("money", "rendered"),
        [
            (Money(1999, USD), "19.99 USD"),
            (Money(-1999, USD), "-19.99 USD"),
            (Money(5, USD), "0.05 USD"),
            (Money(0, USD), "0.00 USD"),
            (Money(500, JPY), "500 JPY"),
            (Money(-7, JPY), "-7 JPY"),
            (Money(1234, KWD), "1.234 KWD"),
        ],
    )
    def test_str(self, money: Money, rendered: str) -> None:
        assert str(money) == rendered

    def test_parse_roundtrips_str(self) -> None:
        for minor in (0, 1, 99, 100, 123456, -1, -100):
            m = Money(minor, USD)
            assert Money.parse(str(m).split()[0], USD) == m


class TestMoneyArithmetic:
    def test_add_sub_neg_abs(self) -> None:
        a, b = Money(1000, USD), Money(250, USD)
        assert a + b == Money(1250, USD)
        assert a - b == Money(750, USD)
        assert -a == Money(-1000, USD)
        assert abs(-a) == a

    def test_mixed_currency_refused(self) -> None:
        with pytest.raises(CurrencyMismatchError) as exc:
            Money(1, USD) + Money(1, EUR)
        assert (exc.value.left, exc.value.right) == ("USD", "EUR")
        with pytest.raises(CurrencyMismatchError):
            _ = Money(1, USD) < Money(1, EUR)

    def test_ordering(self) -> None:
        assert Money(1, USD) < Money(2, USD) <= Money(2, USD)
        assert Money(3, USD) > Money(2, USD) >= Money(2, USD)
        assert Money(5, USD).is_positive and Money(-5, USD).is_negative

    def test_total(self) -> None:
        assert total([Money(1, USD), Money(2, USD)], USD) == Money(3, USD)
        assert total([], USD) == Money.zero(USD)

    def test_scale(self) -> None:
        assert Money(1000, USD).scale(Fraction(3, 2)) == Money(1500, USD)
        assert Money(1000, USD).scale(2) == Money(2000, USD)
        # 1000 * 1/3 = 333.33.. -> 333
        assert Money(1000, USD).scale(Fraction(1, 3)) == Money(333, USD)

    def test_convert_accounts_for_exponents(self) -> None:
        # $10.00 at 150 JPY/USD is ¥1500, not ¥150000.
        assert Money(1000, USD).convert(Fraction(150), JPY) == Money(1500, JPY)
        # ¥1500 at 1/150 USD/JPY is $10.00.
        assert Money(1500, JPY).convert(Fraction(1, 150), USD) == Money(1000, USD)
        # 1 KWD = 3.25 USD -> 1.000 KWD = 3.25 USD
        assert Money(1000, KWD).convert(Fraction(13, 4), USD) == Money(325, USD)

    def test_convert_rejects_nonpositive_rate(self) -> None:
        with pytest.raises(InvalidAmountError):
            Money(1, USD).convert(Fraction(0), EUR)
        with pytest.raises(InvalidAmountError):
            Money(1, USD).convert(Fraction(-1), EUR)


class TestRounding:
    @pytest.mark.parametrize(
        ("value", "mode", "expected"),
        [
            (Fraction(5, 2), RoundingMode.HALF_EVEN, 2),
            (Fraction(7, 2), RoundingMode.HALF_EVEN, 4),
            (Fraction(-5, 2), RoundingMode.HALF_EVEN, -2),
            (Fraction(5, 2), RoundingMode.HALF_UP, 3),
            (Fraction(-5, 2), RoundingMode.HALF_UP, -3),
            (Fraction(5, 2), RoundingMode.HALF_DOWN, 2),
            (Fraction(-5, 2), RoundingMode.HALF_DOWN, -2),
            (Fraction(7, 3), RoundingMode.HALF_UP, 2),
            (Fraction(8, 3), RoundingMode.HALF_DOWN, 3),
            (Fraction(8, 3), RoundingMode.HALF_EVEN, 3),
            (Fraction(7, 3), RoundingMode.HALF_EVEN, 2),
            (Fraction(7, 3), RoundingMode.DOWN, 2),
            (Fraction(-7, 3), RoundingMode.DOWN, -2),
            (Fraction(7, 3), RoundingMode.UP, 3),
            (Fraction(-7, 3), RoundingMode.UP, -3),
            (Fraction(-7, 3), RoundingMode.FLOOR, -3),
            (Fraction(7, 3), RoundingMode.CEILING, 3),
            (Fraction(4), RoundingMode.HALF_UP, 4),
        ],
    )
    def test_modes(self, value: Fraction, mode: RoundingMode, expected: int) -> None:
        assert round_fraction(value, mode) == expected

    def test_default_is_half_even(self) -> None:
        assert round_fraction(Fraction(5, 2)) == 2


class TestAllocation:
    def test_equal_split_distributes_remainder_to_earliest(self) -> None:
        assert Money(100, USD).split(3) == (Money(34, USD), Money(33, USD), Money(33, USD))

    def test_ratios(self) -> None:
        parts = Money(1000, USD).allocate([70, 30])
        assert parts == (Money(700, USD), Money(300, USD))

    def test_largest_remainder_wins(self) -> None:
        # 100 split 1:1:1 -> 33.33 each; two remainders tie, first two get the extra cent.
        # 5 split 1:2 -> 1.67 : 3.33 -> floors 1, 3 ; remainder .67 > .33 -> first gets it.
        assert Money(5, USD).allocate([1, 2]) == (Money(2, USD), Money(3, USD))

    def test_negative_amount(self) -> None:
        assert Money(-100, USD).split(3) == (Money(-34, USD), Money(-33, USD), Money(-33, USD))

    def test_zero_ratio_gets_nothing(self) -> None:
        assert Money(100, USD).allocate([1, 0]) == (Money(100, USD), Money(0, USD))

    @pytest.mark.parametrize("ratios", [[], [0, 0], [1, -1]])
    def test_rejects_bad_ratios(self, ratios: list[int]) -> None:
        with pytest.raises(InvalidAmountError):
            Money(100, USD).allocate(ratios)

    def test_split_rejects_zero_parts(self) -> None:
        with pytest.raises(InvalidAmountError):
            Money(100, USD).split(0)
