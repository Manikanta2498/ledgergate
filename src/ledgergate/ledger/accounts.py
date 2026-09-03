# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Accounts and the chart that names them.

Balances are stored debit-positive internally. An account's :class:`AccountType` says
which side is *normal* for it, so a liability with a credit balance reads as positive to
a human while still being a negative number in the debit-positive store.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TypeVar

from ledgergate.ledger.errors import (
    ConflictingCurrencyError,
    DuplicateAccountError,
    UnknownAccountError,
)
from ledgergate.ledger.identifiers import require_identifier
from ledgergate.ledger.money import Currency

K = TypeVar("K")
V = TypeVar("V")


def freeze(mapping: Mapping[K, V]) -> Mapping[K, V]:
    """A read-only view over a private copy.

    Always copies, even when handed a ``MappingProxyType``: a proxy is only a view, and
    whoever owns its backing dict can still write through it. Trusting an incoming proxy
    would let a caller hand the ledger a mapping and keep a pen.
    """
    return MappingProxyType(dict(mapping))


class Side(Enum):
    DEBIT = "debit"
    CREDIT = "credit"

    @property
    def opposite(self) -> Side:
        return Side.CREDIT if self is Side.DEBIT else Side.DEBIT

    @property
    def sign(self) -> int:
        """Debit-positive convention: debits are +1, credits are -1."""
        return 1 if self is Side.DEBIT else -1


class AccountType(Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"

    @property
    def normal_side(self) -> Side:
        """The side on which this kind of account normally carries its balance."""
        return Side.DEBIT if self in (AccountType.ASSET, AccountType.EXPENSE) else Side.CREDIT


@dataclass(frozen=True, slots=True)
class Account:
    """A single account in a single currency.

    ``allow_negative`` is whether the account may carry a balance on the wrong side of
    zero (in normal-side terms). A customer wallet asset should say ``False`` so an
    over-refund fails loudly; a clearing or equity account usually says ``True``.
    """

    account_id: str
    kind: AccountType
    currency: Currency
    allow_negative: bool = True
    name: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.account_id, "account id")


class ChartOfAccounts(Mapping[str, Account]):
    """An immutable, ordered set of accounts indexed by id."""

    __slots__ = ("_accounts",)

    def __init__(self, accounts: Iterable[Account]) -> None:
        index: dict[str, Account] = {}
        exponents: dict[str, int] = {}
        for account in accounts:
            if account.account_id in index:
                raise DuplicateAccountError(account.account_id)
            # Two Currency objects with the same code and different exponents would make
            # "1000 CAD" mean two different amounts inside one ledger.
            code, exponent = account.currency.code, account.currency.exponent
            if exponents.setdefault(code, exponent) != exponent:
                raise ConflictingCurrencyError(code, (exponents[code], exponent))
            index[account.account_id] = account
        self._accounts: Mapping[str, Account] = freeze(index)

    def __getitem__(self, account_id: str) -> Account:
        try:
            return self._accounts[account_id]
        except KeyError:
            raise UnknownAccountError(account_id) from None

    def __iter__(self) -> Iterator[str]:
        return iter(self._accounts)

    def __len__(self) -> int:
        return len(self._accounts)

    def __repr__(self) -> str:
        return f"ChartOfAccounts({list(self._accounts)!r})"

    def currencies(self) -> dict[str, Currency]:
        """Every currency in the chart, by code. Consistent by construction."""
        return {a.currency.code: a.currency for a in self._accounts.values()}

    def of_type(self, kind: AccountType) -> tuple[Account, ...]:
        return tuple(a for a in self._accounts.values() if a.kind is kind)
