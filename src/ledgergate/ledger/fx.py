# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Cross-currency movements that still balance.

A journal entry balances *per currency*. Moving value from a USD account to a EUR account
therefore needs four postings through a pair of clearing accounts, one per currency: the
USD leg balances in USD, the EUR leg balances in EUR, and the clearing accounts carry the
open FX position. This module builds that entry so callers cannot get it subtly wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from ledgergate.ledger.effects import FxRateSource
from ledgergate.ledger.entries import EntryDraft, credit, debit
from ledgergate.ledger.errors import InvalidAmountError
from ledgergate.ledger.money import Currency, Money, RoundingMode


@dataclass(frozen=True, slots=True)
class Conversion:
    """The result of pricing a conversion: what leaves, what arrives, and at what rate."""

    source: Money
    destination: Money
    rate_numerator: int
    rate_denominator: int

    @property
    def rate(self) -> str:
        return f"{self.rate_numerator}/{self.rate_denominator}"


def price(
    money: Money,
    to: Currency,
    rates: FxRateSource,
    rounding: RoundingMode = RoundingMode.HALF_EVEN,
) -> Conversion:
    """Price ``money`` in ``to`` using ``rates``. Same currency is the identity.

    The identity case never consults the rate source: a source that returns anything
    but 1 for USD->USD is broken, and the ledger should not be the thing that finds out.
    """
    rate = Fraction(1) if money.currency == to else rates.rate(money.currency, to)
    return Conversion(money, money.convert(rate, to, rounding), rate.numerator, rate.denominator)


def conversion_entry(
    money: Money,
    to: Currency,
    rates: FxRateSource,
    *,
    source_account: str,
    destination_account: str,
    source_clearing: str,
    destination_clearing: str,
    rounding: RoundingMode = RoundingMode.HALF_EVEN,
    description: str = "",
) -> EntryDraft:
    """Build a balanced four-line entry moving ``money`` into ``to``.

    ::

        credit source_account        (source currency)
        debit  source_clearing       (source currency)
        credit destination_clearing  (destination currency)
        debit  destination_account   (destination currency)

    If the currencies are the same the clearing legs are skipped and a plain two-line
    transfer is returned, because a same-currency "conversion" through clearing would
    only add noise.
    """
    if not money.is_positive:
        raise InvalidAmountError(f"conversion amount must be positive, got {money}")
    priced = price(money, to, rates, rounding)
    label = description or f"convert {priced.source} to {priced.destination} @ {priced.rate}"

    if money.currency == to:
        return EntryDraft.of(
            credit(source_account, money),
            debit(destination_account, money),
            description=label,
            fx_rate=priced.rate,
        )
    if not priced.destination.is_positive:
        raise InvalidAmountError(
            f"{money} converts to {priced.destination}; below one minor unit of {to.code}"
        )
    return EntryDraft.of(
        credit(source_account, priced.source),
        debit(source_clearing, priced.source),
        credit(destination_clearing, priced.destination),
        debit(destination_account, priced.destination),
        description=label,
        fx_rate=priced.rate,
    )
