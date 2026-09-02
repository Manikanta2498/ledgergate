"""Properties of Money that must hold for *every* amount, not just the ones in unit tests."""

from __future__ import annotations

from fractions import Fraction

from hypothesis import given, settings
from hypothesis import strategies as st

from ledgergate.ledger import CURRENCIES, Currency, Money, RoundingMode, round_fraction

currencies = st.sampled_from(sorted(CURRENCIES.values(), key=lambda c: c.code))
amounts = st.integers(min_value=-(10**15), max_value=10**15)
ratios = st.lists(st.integers(min_value=0, max_value=1_000), min_size=1, max_size=12).filter(
    lambda r: sum(r) > 0
)
fractions = st.fractions(
    min_value=Fraction(-(10**9)), max_value=Fraction(10**9), max_denominator=10**6
)


@given(amounts, currencies, ratios)
def test_allocation_is_exact(amount: int, cur: Currency, weights: list[int]) -> None:
    """Parts always sum to the whole; no minor unit is created or lost."""
    parts = Money(amount, cur).allocate(weights)
    assert len(parts) == len(weights)
    assert sum(p.amount for p in parts) == amount
    assert all(p.currency == cur for p in parts)


@given(amounts, currencies, ratios)
def test_allocation_is_proportional_within_one_unit(
    amount: int, cur: Currency, weights: list[int]
) -> None:
    """Each part is within one minor unit of its exact share."""
    total = sum(weights)
    for part, weight in zip(Money(amount, cur).allocate(weights), weights, strict=True):
        exact = Fraction(amount * weight, total)
        assert abs(Fraction(part.amount) - exact) < 1


@given(amounts, currencies, st.integers(min_value=1, max_value=50))
def test_split_parts_differ_by_at_most_one(amount: int, cur: Currency, n: int) -> None:
    parts = [p.amount for p in Money(amount, cur).split(n)]
    assert sum(parts) == amount
    assert max(parts) - min(parts) <= 1


@given(amounts, currencies)
def test_format_parse_roundtrip(amount: int, cur: Currency) -> None:
    """str() is a faithful, parseable rendering."""
    money = Money(amount, cur)
    rendered = str(money)
    assert rendered.endswith(" " + cur.code)
    assert Money.parse(rendered.removesuffix(" " + cur.code), cur) == money


@given(fractions, st.sampled_from(list(RoundingMode)))
def test_rounding_is_within_one_of_the_value(value: Fraction, mode: RoundingMode) -> None:
    rounded = round_fraction(value, mode)
    assert abs(Fraction(rounded) - value) < 1
    if mode in (RoundingMode.HALF_EVEN, RoundingMode.HALF_UP, RoundingMode.HALF_DOWN):
        assert abs(Fraction(rounded) - value) <= Fraction(1, 2)


@given(fractions)
def test_rounding_modes_bracket_the_value(value: Fraction) -> None:
    """FLOOR <= every mode <= CEILING, and DOWN/UP are the toward/away-from-zero pair."""
    results = {mode: round_fraction(value, mode) for mode in RoundingMode}
    assert results[RoundingMode.FLOOR] <= min(results.values())
    assert results[RoundingMode.CEILING] >= max(results.values())
    assert abs(results[RoundingMode.DOWN]) <= abs(results[RoundingMode.UP])


@given(amounts, amounts, currencies)
def test_add_sub_are_inverse(a: int, b: int, cur: Currency) -> None:
    x, y = Money(a, cur), Money(b, cur)
    assert (x + y) - y == x
    assert x.__neg__().__neg__() == x
    assert abs(x).amount == abs(a)


@settings(max_examples=200)
@given(
    st.integers(min_value=1, max_value=10**9),
    st.fractions(min_value=Fraction(1, 10**4), max_value=Fraction(10**4), max_denominator=10**4),
)
def test_convert_round_trip_error_is_bounded(amount: int, rate: Fraction) -> None:
    """Converting there and back loses at most one minor unit per rounding step, scaled."""
    usd, jpy = CURRENCIES["USD"], CURRENCIES["JPY"]
    there = Money(amount, usd).convert(rate, jpy)
    back = there.convert(1 / rate, usd)
    # One JPY of rounding error becomes at most (1/rate) USD major = 100/rate minor units,
    # plus one unit of rounding on the way back.
    tolerance = Fraction(100) / rate + 1
    assert abs(back.amount - amount) <= tolerance
