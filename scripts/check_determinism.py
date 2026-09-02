#!/usr/bin/env python3
"""Fail if the ledger core reaches for a non-deterministic source.

Reproducible replay is the foundation of LedgerGate's `$0 CI` claim and of every
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

# Fully-qualified dotted paths of wall-clock and entropy calls. Matched against the
# *resolved* path of a call, so `from datetime import datetime as DT; DT.now()` and
# `import time as t; t.monotonic()` are caught, not only the literal spelling.
CLOCK = "take the timestamp from the injected Clock"
IDS = "identifiers must come from the injected IdGenerator"
BANNED_CALLS = {
    "datetime.datetime.now": CLOCK,
    "datetime.datetime.today": CLOCK,
    "datetime.datetime.utcnow": CLOCK,
    "datetime.date.today": CLOCK,
    "time.time": CLOCK,
    "time.time_ns": CLOCK,
    "time.monotonic": CLOCK,
    "time.monotonic_ns": CLOCK,
    "time.perf_counter": CLOCK,
    "time.perf_counter_ns": CLOCK,
    "time.process_time": CLOCK,
    "uuid.uuid1": IDS,
    "uuid.uuid4": IDS,
}


FLOAT_MESSAGE = "money is never a float; use integer minor units and Fraction for rates"


# Names bound to one of these roots are tracked through aliases and assignments.
TRACKED_ROOTS = frozenset({"datetime", "time", "uuid", "random", "secrets"})

SHADOW_MESSAGE = (
    "rebinds a name that elsewhere refers to {paths}; pick a different name so the gate"
    " can tell a clock from a parameter"
)


def _dotted(node: ast.expr) -> tuple[str, list[str]] | None:
    """Split ``a.b.c`` into its root name and attribute chain."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.reverse()
    return node.id, parts


class _Taint:
    """Whole-file, scope-blind name tracking. Sound by construction, not precise.

    A precise resolver has to model function scopes, class scopes, branches, execution
    order, ``global``, ``nonlocal`` and comprehension semantics, and every place it gets
    one of those slightly wrong is a way past the gate. This deliberately does none of
    that. A name bound *anywhere* in the file to a tracked module keeps that binding
    *everywhere*, and binding such a name to anything else is itself reported.

    The cost is a false positive when someone names a parameter ``dt`` in a file that also
    has ``from datetime import datetime as dt``. The fix for that is a rename, which the
    message says. The alternative cost -- accepting a wall-clock call because a class body
    or a dead ``if`` rebinding fooled the resolver -- is the thing the gate exists to stop.
    """

    def __init__(self) -> None:
        # name -> every tracked fully-qualified path it is bound to anywhere in the file
        self.tracked: dict[str, set[str]] = {}
        # name -> line of a binding to something that is *not* tracked
        self.plain: dict[str, int] = {}

    def paths(self, name: str) -> set[str]:
        return self.tracked.get(name, set())

    def resolve(self, node: ast.expr) -> set[str]:
        """Every tracked fully-qualified path a call target could refer to."""
        split = _dotted(node)
        if split is None:
            return set()
        root, parts = split
        return {".".join([base, *parts]) for base in self.paths(root)}

    def _bind(self, name: str, path: str | None, lineno: int) -> bool:
        """Record a binding. Returns True if the tracked table changed."""
        if path is not None and path.split(".")[0] in TRACKED_ROOTS:
            bucket = self.tracked.setdefault(name, set())
            if path in bucket:
                return False
            bucket.add(path)
            return True
        self.plain.setdefault(name, lineno)
        return False

    def collect(self, tree: ast.Module) -> None:
        # Imports first: they are the roots everything else resolves through.
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self._bind(alias.asname, alias.name, node.lineno)
                    else:
                        root = alias.name.split(".")[0]
                        self._bind(root, root, node.lineno)
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    self._bind(
                        alias.asname or alias.name, f"{node.module}.{alias.name}", node.lineno
                    )

        # Assignments of the form `name = <name or dotted path>` propagate tracked paths.
        # Iterate to a fixpoint so `a = datetime; b = a; c = b.now` all land, in any order.
        aliases = [
            (target, node.value, node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
            and node.value is not None
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
        ]
        changed = True
        while changed:
            changed = False
            for target, value, lineno in aliases:
                resolved = (
                    self.resolve(value) if isinstance(value, (ast.Name, ast.Attribute)) else set()
                )
                if resolved:
                    for path in resolved:
                        changed |= self._bind(target.id, path, lineno)
                else:
                    self._bind(target.id, None, lineno)

        # Every other way a name can be bound is a plain binding: parameters, def/class
        # names, loop and with targets, unpacking, except-as, comprehension variables.
        alias_targets = {id(target) for target, _, _ in aliases}
        for node in ast.walk(tree):
            if isinstance(node, ast.arg):
                self.plain.setdefault(node.arg, node.lineno)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.plain.setdefault(node.name, node.lineno)
            elif isinstance(node, ast.ExceptHandler):
                if node.name is not None:
                    self.plain.setdefault(node.name, node.lineno)
            elif (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Store)
                and id(node) not in alias_targets
            ):
                # Direct alias targets were classified above; everything else is plain.
                self.plain.setdefault(node.id, node.lineno)


def check_file(path: Path) -> list[str]:
    """Return a list of human-readable violations found in one file."""
    violations: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    taint = _Taint()
    taint.collect(tree)

    for name, lineno in sorted(taint.plain.items(), key=lambda item: item[1]):
        if name in taint.tracked:
            paths = ", ".join(sorted(taint.tracked[name]))
            violations.append(f"{path}:{lineno}: {name!r} {SHADOW_MESSAGE.format(paths=paths)}")

    # A banned callable is refused at the point it is *referenced*, not only where it is
    # called. Otherwise every way a value can flow -- tuple unpacking, default arguments,
    # list literals, dict values, keyword arguments -- is a place the call-site check can
    # be routed around. Refusing the reference closes all of them at once.
    call_targets = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}

    for node in ast.walk(tree):
        # Floats are banned outright: a literal, a float() call, or a float annotation.
        # Binary floating point cannot represent 0.10, and a ledger that rounds
        # differently on two machines is not deterministic.
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            violations.append(
                f"{path}:{node.lineno}: float literal {node.value!r} -- {FLOAT_MESSAGE}"
            )
        elif isinstance(node, ast.Name) and node.id == "float":
            violations.append(f"{path}:{node.lineno}: uses 'float' -- {FLOAT_MESSAGE}")
        elif isinstance(node, ast.Import):
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
            for target in sorted(taint.resolve(node.func) & BANNED_CALLS.keys()):
                spelled = ast.unparse(node.func)
                seen = f"{spelled}()" if spelled == target else f"{spelled}() [= {target}]"
                violations.append(f"{path}:{node.lineno}: calls {seen} -- {BANNED_CALLS[target]}")
        elif (
            isinstance(node, (ast.Attribute, ast.Name))
            and isinstance(node.ctx, ast.Load)
            and id(node) not in call_targets
        ):
            for target in sorted(taint.resolve(node) & BANNED_CALLS.keys()):
                spelled = ast.unparse(node)
                seen = spelled if spelled == target else f"{spelled} [= {target}]"
                violations.append(
                    f"{path}:{node.lineno}: takes a reference to {seen} -- {BANNED_CALLS[target]}"
                )

    return sorted(violations, key=lambda v: int(v.split(":")[1]))


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
