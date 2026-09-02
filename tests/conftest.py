"""Shared fixtures: a small chart of accounts and deterministic effects."""

from __future__ import annotations

import pytest

from ledgergate.ledger import (
    EPOCH,
    EUR,
    USD,
    Account,
    AccountType,
    ChartOfAccounts,
    FixedClock,
    Ledger,
    SequentialIds,
)


@pytest.fixture
def chart() -> ChartOfAccounts:
    return ChartOfAccounts(
        [
            Account("cash", AccountType.ASSET, USD, name="Operating cash"),
            Account("cash:eur", AccountType.ASSET, EUR),
            Account("wallet:alice", AccountType.LIABILITY, USD, allow_negative=False),
            Account("revenue", AccountType.REVENUE, USD),
            Account("fees", AccountType.EXPENSE, USD),
            Account("equity", AccountType.EQUITY, USD),
            Account("fx:usd", AccountType.ASSET, USD),
            Account("fx:eur", AccountType.ASSET, EUR),
        ]
    )


@pytest.fixture
def ledger(chart: ChartOfAccounts) -> Ledger:
    return Ledger.empty(chart)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(EPOCH)


@pytest.fixture
def ids() -> SequentialIds:
    return SequentialIds()
