# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Every effect the ledger needs, as a Protocol the caller supplies.

The core never reads a wall clock, never generates an identifier and never fetches a
rate. It is handed all three. That is what makes a replay hash-identical: feed the same
effects, get the same ledger. ``scripts/check_determinism.py`` fails CI if anything in
this package tries to reach around these interfaces.

The concrete classes here are deterministic reference implementations, suitable for
tests, replay and any caller that wants reproducibility over realism.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Protocol, runtime_checkable

from ledgergate.ledger.errors import InvalidAmountError, MissingRateError
from ledgergate.ledger.money import Currency


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current time. Must be timezone-aware."""
        ...


@runtime_checkable
class IdGenerator(Protocol):
    def next_id(self) -> str:
        """A fresh identifier. Uniqueness is the implementation's promise."""
        ...


@runtime_checkable
class FxRateSource(Protocol):
    def rate(self, base: Currency, quote: Currency) -> Fraction:
        """Units of ``quote`` per one major unit of ``base``, as an exact rational."""
        ...


# ------------------------------------------------------------- reference impls


class FixedClock:
    """Always returns the same instant. The simplest possible deterministic clock."""

    __slots__ = ("_at",)

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise InvalidAmountError("FixedClock needs a timezone-aware datetime")
        self._at = at

    def now(self) -> datetime:
        return self._at


class SteppingClock:
    """Advances by a fixed step on every call, so successive entries have distinct times."""

    __slots__ = ("_next", "_step")

    def __init__(self, start: datetime, step: timedelta = timedelta(seconds=1)) -> None:
        if start.tzinfo is None:
            raise InvalidAmountError("SteppingClock needs a timezone-aware datetime")
        if step <= timedelta(0):
            raise InvalidAmountError(f"SteppingClock step must be positive, got {step}")
        self._next, self._step = start, step

    def now(self) -> datetime:
        current, self._next = self._next, self._next + self._step
        return current


class SequentialIds:
    """``prefix-000001``, ``prefix-000002``, ... Deterministic and human-sortable."""

    __slots__ = ("_counter", "_prefix", "_width")

    def __init__(self, prefix: str = "e", start: int = 1, width: int = 6) -> None:
        self._prefix, self._counter, self._width = prefix, start, width

    def next_id(self) -> str:
        value, self._counter = self._counter, self._counter + 1
        return f"{self._prefix}-{value:0{self._width}d}"


class StaticRates:
    """A fixed table of rates. Inverse pairs are derived exactly if only one is given."""

    __slots__ = ("_table",)

    def __init__(self, table: Mapping[tuple[Currency, Currency], Fraction]) -> None:
        for (base, quote), value in table.items():
            if value <= 0:
                raise InvalidAmountError(f"rate {base}->{quote} must be positive, got {value}")
        self._table = dict(table)

    def rate(self, base: Currency, quote: Currency) -> Fraction:
        if base == quote:
            return Fraction(1)
        if (base, quote) in self._table:
            return self._table[(base, quote)]
        if (quote, base) in self._table:
            return 1 / self._table[(quote, base)]
        raise MissingRateError(base.code, quote.code)


EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
"""A conventional fixed instant for tests and examples."""
