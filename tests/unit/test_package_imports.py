"""Every module in the package must import cleanly.

This is cheap insurance against circular imports, which are easy to introduce once the
layers fill in and which otherwise surface only at CLI startup. `import-linter` checks
the direction of dependencies; this checks that they resolve at all.
"""

from __future__ import annotations

import importlib
import pkgutil

import ledgergate


def iter_module_names() -> list[str]:
    """Return every importable module name under the ledgergate package."""
    return sorted(
        module.name for module in pkgutil.walk_packages(ledgergate.__path__, prefix="ledgergate.")
    )


def test_package_tree_is_discoverable() -> None:
    names = iter_module_names()
    expected = {
        "ledgergate.adapters",
        "ledgergate.cli",
        "ledgergate.idempotency",
        "ledgergate.invariants",
        "ledgergate.ledger",
        "ledgergate.redaction",
        "ledgergate.report",
        "ledgergate.runner",
    }
    assert expected.issubset(set(names))


def test_every_module_imports() -> None:
    for name in iter_module_names():
        importlib.import_module(name)


def test_package_is_typed() -> None:
    marker = next(iter(ledgergate.__path__)) + "/py.typed"
    from pathlib import Path

    assert Path(marker).is_file(), "py.typed marker missing; downstream mypy would skip us"
