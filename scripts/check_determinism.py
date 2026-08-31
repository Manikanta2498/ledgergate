#!/usr/bin/env python3
"""Fail if the ledger core reaches for a non-deterministic source.

Byte-reproducible replay is the foundation of LedgerGate's `$0 CI` claim and of every
model-drift comparison. A single ``datetime.now()`` or ``uuid4()`` inside the ledger core
silently breaks it, and nothing else in the toolchain catches that: mypy sees a valid call
and the tests still pass, because the drift only shows up when two runs are diffed.

So this runs as its own CI gate. The core takes its clock and its identifiers by
injection (see ``ledgergate.ledger``); this script proves it stayed that way.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

LEDGER = Path(__file__).resolve().parent.parent / "src" / "ledgergate" / "ledger"

BANNED_MODULES = {
    "random": "seed and inject the source, or use a deterministic sequence",
    "secrets": "identifiers must come from the injected IdGenerator",
    "uuid": "identifiers must come from the injected IdGenerator",
}

BANNED_CALLS = {
    ("datetime", "now"): "take the timestamp from the injected Clock",
    ("datetime", "today"): "take the timestamp from the injected Clock",
    ("datetime", "utcnow"): "take the timestamp from the injected Clock",
    ("time", "time"): "take the timestamp from the injected Clock",
    ("time", "monotonic"): "take the timestamp from the injected Clock",
    ("uuid", "uuid1"): "identifiers must come from the injected IdGenerator",
    ("uuid", "uuid4"): "identifiers must come from the injected IdGenerator",
}


def check_file(path: Path) -> list[str]:
    """Return a list of human-readable violations found in one file."""
    violations: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BANNED_MODULES:
                    violations.append(
                        f"{path}:{node.lineno}: imports '{alias.name}' -- {BANNED_MODULES[root]}"
                    )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BANNED_MODULES:
                names = ", ".join(a.name for a in node.names)
                violations.append(
                    f"{path}:{node.lineno}: imports {names} from '{node.module}'"
                    f" -- {BANNED_MODULES[root]}"
                )
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                key = (func.value.id, func.attr)
                if key in BANNED_CALLS:
                    call = f"{key[0]}.{key[1]}()"
                    violations.append(f"{path}:{node.lineno}: calls {call} -- {BANNED_CALLS[key]}")

    return violations


def main() -> int:
    """Scan the ledger package. Returns the process exit code."""
    if not LEDGER.is_dir():
        print(f"determinism: ledger package not found at {LEDGER}", file=sys.stderr)
        return 1

    violations: list[str] = []
    files = sorted(LEDGER.rglob("*.py"))
    for path in files:
        violations.extend(check_file(path))

    if violations:
        print("Determinism check FAILED. The ledger core must be reproducible.\n")
        for line in violations:
            print(f"  {line}")
        print(f"\n{len(violations)} violation(s) across {len(files)} file(s).")
        return 1

    print(f"determinism: OK ({len(files)} file(s) in the ledger core, no banned sources)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
