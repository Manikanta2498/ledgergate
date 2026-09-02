"""The README example must actually run.

Documentation that drifts from the code is a claim the gates do not prove. This extracts
the ledger-core block from README.md and executes it, then checks the two error cases the
block shows as comments really raise with the messages shown.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ledgergate.ledger import (
    EPOCH,
    USD,
    Account,
    AccountType,
    ChartOfAccounts,
    EntryDraft,
    FixedClock,
    IllegalTransitionError,
    Ledger,
    Money,
    OpenTransaction,
    Refund,
    SequentialIds,
    UnbalancedEntryError,
    credit,
    debit,
)

README = Path(__file__).resolve().parents[2] / "README.md"
ERROR_CASES_MARKER = "# Unbalanced entries cannot be constructed"


def readme_block() -> str:
    text = README.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", text, re.DOTALL)
    assert match, "README has no python block"
    return match.group(1)


def test_readme_happy_path_runs() -> None:
    block = readme_block()
    assert ERROR_CASES_MARKER in block
    runnable = block.split(ERROR_CASES_MARKER)[0]
    namespace: dict[str, object] = {}
    exec(compile(runnable, str(README), "exec"), namespace)
    assert namespace["retry"].replayed  # type: ignore[attr-defined]


def test_readme_error_cases_raise_as_documented() -> None:
    block = readme_block()
    with pytest.raises(UnbalancedEntryError, match=r"entry does not balance \(USD: \+1\)"):
        EntryDraft.of(debit("cash", Money(100, USD)), credit("revenue", Money(99, USD)))
    assert "UnbalancedEntryError: entry does not balance (USD: +1)" in block

    chart = ChartOfAccounts([Account("cash", AccountType.ASSET, USD)])
    clock, ids = FixedClock(EPOCH), SequentialIds()
    opened = Ledger.empty(chart).execute(
        OpenTransaction("o", "t", Money(1, USD)), clock=clock, ids=ids
    )
    with pytest.raises(IllegalTransitionError, match="in pending cannot accept refund"):
        opened.ledger.execute(Refund("r", "t", Money(1, USD)), clock=clock, ids=ids)
    assert "transaction 't' in pending cannot accept refund" in block
