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


def readme_blocks() -> list[str]:
    text = README.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    assert len(blocks) >= 4, "README shows the trace schema, the journal, redaction and the core"
    return blocks


def readme_block() -> str:
    """The ledger-core block: the one that demonstrates the documented error cases."""
    return next(b for b in readme_blocks() if ERROR_CASES_MARKER in b)


@pytest.mark.parametrize("index", range(4))
def test_every_readme_block_runs(index: int, tmp_path: Path) -> None:
    """Each block is executed up to the point where it deliberately demonstrates errors.
    The journal block expects a ``path`` binding, which the README leaves to the reader."""
    block = readme_blocks()[index]
    runnable = block.split(ERROR_CASES_MARKER)[0]
    namespace: dict[str, object] = {
        "path": str(tmp_path / "readme.journal"),
        "path2": str(tmp_path / "readme2.journal"),
        "key_bytes": bytes(range(32)),
    }
    exec(compile(runnable, str(README), "exec"), namespace)
    # Each block ends in assertions; reaching here means they held.
    assert namespace, "block produced no bindings"


def test_readme_happy_path_runs() -> None:
    namespace: dict[str, object] = {}
    exec(compile(readme_block().split(ERROR_CASES_MARKER)[0], str(README), "exec"), namespace)
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
