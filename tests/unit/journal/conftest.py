"""Fixtures for journal tests: a fresh journal per test and a raw inspection connection."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from tests.unit.journal.support import CHART

from ledgergate.journal import Journal
from ledgergate.ledger import EPOCH, SequentialIds, SteppingClock


@pytest.fixture
def journal_path(tmp_path: Path) -> str:
    return str(tmp_path / "ledger.journal")


@pytest.fixture
def journal(journal_path: str) -> Iterator[Journal]:
    j = Journal.create(journal_path, CHART, clock=SteppingClock(EPOCH), ids=SequentialIds())
    yield j
    j.close()


@pytest.fixture
def reopen(journal_path: str) -> Callable[[], Journal]:
    def _reopen() -> Journal:
        return Journal.open(journal_path, clock=SteppingClock(EPOCH), ids=SequentialIds(start=1000))

    return _reopen


@pytest.fixture
def raw(journal_path: str) -> Iterator[sqlite3.Connection]:
    """A second, independent connection for inspecting rows and probing constraints."""
    conn = sqlite3.connect(journal_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()
