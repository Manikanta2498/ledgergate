"""Shared fixtures: a small chart of accounts and deterministic effects."""

from __future__ import annotations

import os

import pytest
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings

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

# docs/spec/assurance.md, *The mutation gate*: Hypothesis's built-in `ci` profile (derandomize,
# no example database, no deadline) is what Hypothesis loads under CI; mutmut runs the tests
# with MUTANT_UNDER_TEST in the environment (empty for the clean run), and a survivor must be
# reproducible, so the same profile is loaded then too. Presence, not truthiness.
# mutmut calls each test from its own collector as well as from pytest, which Hypothesis's
# `differing_executors` health check (rightly, in general) refuses; under mutmut the profile is
# `ci` plus that one suppression, and nothing else differs.
hypothesis_settings.register_profile(
    "mutation",
    parent=hypothesis_settings.get_profile("ci"),
    suppress_health_check=[
        *hypothesis_settings.get_profile("ci").suppress_health_check,
        HealthCheck.differing_executors,
    ],
)
if "MUTANT_UNDER_TEST" in os.environ:
    hypothesis_settings.load_profile("mutation")
elif os.environ.get("CI"):
    hypothesis_settings.load_profile("ci")


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
