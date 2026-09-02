# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Money as an integer count of minor units in a named currency.

There is no float anywhere in this module and none can get in: :class:`Money` rejects a
``float`` amount at construction, rates are :class:`fractions.Fraction`, and every
operation that can produce a fractional result takes an explicit :class:`RoundingMode`.
Allocation uses the largest-remainder method so the parts always sum to the whole.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction

from ledgergate.ledger.errors import CurrencyMismatchError, InvalidAmountError

_CODE = re.compile(r"^[A-Z]{3}$")
_DECIMAL = re.compile(r"^(?P<sign>[-+]?)(?P<int>\d+)(?:\.(?P<frac>\d*))?$")


class RoundingMode(Enum):
    """How a fractional minor-unit result is brought back to an integer.

    ``HALF_EVEN`` is the default because it is unbiased over many operations, which is
    what a ledger that repeatedly converts and allocates needs.
    """

    HALF_EVEN = "half_even"
    HALF_UP = "half_up"
    HALF_DOWN = "half_down"
    DOWN = "down"
    UP = "up"
    FLOOR = "floor"
    CEILING = "ceiling"


def round_fraction(value: Fraction, mode: RoundingMode = RoundingMode.HALF_EVEN) -> int:
    """Round an exact rational to an integer under ``mode``. Never touches a float."""
    if value.denominator == 1:
        return value.numerator
    floor = value.numerator // value.denominator
    remainder = value - floor
    half = Fraction(1, 2)

    match mode:
        case RoundingMode.FLOOR:
            return floor
        case RoundingMode.CEILING:
            return floor + 1
        case RoundingMode.DOWN:
            return floor if value >= 0 else floor + 1
        case RoundingMode.UP:
            return floor + 1 if value >= 0 else floor
        case RoundingMode.HALF_UP:
            if remainder > half:
                return floor + 1
            if remainder < half:
                return floor
            return floor + 1 if value >= 0 else floor
        case RoundingMode.HALF_DOWN:
            if remainder > half:
                return floor + 1
            if remainder < half:
                return floor
            return floor if value >= 0 else floor + 1
        case RoundingMode.HALF_EVEN:
            if remainder > half:
                return floor + 1
            if remainder < half:
                return floor
            return floor if floor % 2 == 0 else floor + 1


@dataclass(frozen=True, slots=True)
class Currency:
    """An ISO 4217 style currency: a three-letter code and its minor-unit exponent."""

    code: str
    exponent: int

    def __post_init__(self) -> None:
        if not _CODE.match(self.code):
            raise InvalidAmountError(
                f"currency code must be three uppercase letters, got {self.code!r}"
            )
        if not 0 <= self.exponent <= 6:
            raise InvalidAmountError(f"currency exponent must be 0..6, got {self.exponent}")

    @property
    def unit(self) -> int:
        """Minor units in one major unit: 100 for USD, 1 for JPY, 1000 for KWD."""
        # int ** int is typed as Any by typeshed because a negative exponent yields a
        # float; the exponent is validated non-negative above, so this is a plain int.
        return int(10**self.exponent)

    def __str__(self) -> str:
        return self.code


USD = Currency("USD", 2)
EUR = Currency("EUR", 2)
GBP = Currency("GBP", 2)
JPY = Currency("JPY", 0)
INR = Currency("INR", 2)
CHF = Currency("CHF", 2)
KWD = Currency("KWD", 3)

CURRENCIES: dict[str, Currency] = {c.code: c for c in (USD, EUR, GBP, JPY, INR, CHF, KWD)}


def currency(code: str) -> Currency:
    """Look up a bundled currency by code."""
    try:
        return CURRENCIES[code]
    except KeyError:
        raise InvalidAmountError(f"unknown currency {code!r}") from None


@dataclass(frozen=True, slots=True)
class Money:
    """An exact amount of one currency, in minor units.

    ``Money(1999, USD)`` is $19.99. Arithmetic is closed over a single currency; mixing
    currencies raises :class:`CurrencyMismatchError` rather than guessing a rate.
    """

    amount: int
    currency: Currency

    def __post_init__(self) -> None:
        # bool is an int subclass and float is the classic bug; reject both by name.
        if isinstance(self.amount, bool) or not isinstance(self.amount, int):
            raise InvalidAmountError(
                f"Money takes an int of minor units, got {type(self.amount).__name__};"
                " money is never a float"
            )

    # ------------------------------------------------------------ construct

    @classmethod
    def zero(cls, currency: Currency) -> Money:
        return cls(0, currency)

    @classmethod
    def parse(cls, text: str, currency: Currency) -> Money:
        """Parse a decimal string such as ``"19.99"`` exactly, without going through float.

        More fractional digits than the currency allows is an error, not a rounding.
        """
        match = _DECIMAL.match(text.strip())
        if match is None:
            raise InvalidAmountError(f"cannot parse {text!r} as a decimal amount")
        frac = match.group("frac") or ""
        if len(frac) > currency.exponent:
            raise InvalidAmountError(
                f"{text!r} has {len(frac)} decimal places;"
                f" {currency.code} allows {currency.exponent}"
            )
        minor = int(match.group("int")) * currency.unit + int(
            frac.ljust(currency.exponent, "0") or "0"
        )
        return cls(-minor if match.group("sign") == "-" else minor, currency)

    # ------------------------------------------------------------- inspect

    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    @property
    def is_positive(self) -> bool:
        return self.amount > 0

    @property
    def is_negative(self) -> bool:
        return self.amount < 0

    def __str__(self) -> str:
        """Deterministic decimal rendering: ``-19.99 USD``, ``500 JPY``."""
        sign = "-" if self.amount < 0 else ""
        major, minor = divmod(abs(self.amount), self.currency.unit)
        if self.currency.exponent == 0:
            return f"{sign}{major} {self.currency.code}"
        return f"{sign}{major}.{minor:0{self.currency.exponent}d} {self.currency.code}"

    # ----------------------------------------------------------- arithmetic

    def _same(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(self.currency.code, other.currency.code)

    def __add__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.amount), self.currency)

    def __lt__(self, other: Money) -> bool:
        self._same(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._same(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._same(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._same(other)
        return self.amount >= other.amount

    def scale(
        self, factor: Fraction | int, rounding: RoundingMode = RoundingMode.HALF_EVEN
    ) -> Money:
        """Multiply by an exact rational factor, rounding once at the end."""
        return Money(
            round_fraction(Fraction(self.amount) * Fraction(factor), rounding), self.currency
        )

    def convert(
        self, rate: Fraction, to: Currency, rounding: RoundingMode = RoundingMode.HALF_EVEN
    ) -> Money:
        """Convert at ``rate`` quote-per-base (1 unit of self.currency = ``rate`` units of ``to``).

        The rate is between *major* units, so the minor-unit exponents are accounted for
        here rather than pushed onto every caller.
        """
        if rate <= 0:
            raise InvalidAmountError(f"exchange rate must be positive, got {rate}")
        major = Fraction(self.amount, self.currency.unit)
        return Money(round_fraction(major * rate * to.unit, rounding), to)

    def allocate(self, ratios: Sequence[int]) -> tuple[Money, ...]:
        """Split proportionally so the parts sum *exactly* to the whole.

        Uses the largest-remainder method: each part gets its floor share, then the
        leftover minor units go one at a time to the parts with the largest remainders,
        earliest first on ties, which keeps the result deterministic.
        """
        if not ratios or any(r < 0 for r in ratios):
            raise InvalidAmountError("allocation ratios must be non-empty and non-negative")
        total = sum(ratios)
        if total == 0:
            raise InvalidAmountError("allocation ratios must not all be zero")

        sign = -1 if self.amount < 0 else 1
        magnitude = abs(self.amount)
        shares = [magnitude * r // total for r in ratios]
        remainders = [
            Fraction(magnitude * r, total) - s for r, s in zip(ratios, shares, strict=True)
        ]
        leftover = magnitude - sum(shares)
        for index in sorted(range(len(ratios)), key=lambda i: (-remainders[i], i))[:leftover]:
            shares[index] += 1
        return tuple(Money(sign * s, self.currency) for s in shares)

    def split(self, parts: int) -> tuple[Money, ...]:
        """Divide into ``parts`` near-equal amounts that sum exactly to the whole."""
        if parts < 1:
            raise InvalidAmountError("split needs at least one part")
        return self.allocate([1] * parts)


def total(values: Sequence[Money], currency: Currency) -> Money:
    """Sum a sequence, with an explicit currency so an empty sequence is well-defined."""
    result = Money.zero(currency)
    for value in values:
        result = result + value
    return result
