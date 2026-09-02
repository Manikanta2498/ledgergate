#!/usr/bin/env python3
"""Fail if any file in a license-critical directory lacks a license declaration.

This repository is deliberately mixed-license: the runtime is BUSL-1.1 and the corpus and
trace schema are Apache-2.0. A reader who lands on a single file from a search result has
no way to tell which applies, and a file that drifts across the boundary during a refactor
would carry the wrong terms silently. A per-file declaration makes the boundary local.

Scope is intentionally narrow. Only the regions whose license is settled are checked:

    src/ledgergate/    BUSL-1.1     (everything, because everything here ships in the wheel)
    corpus/            Apache-2.0
    schema/            Apache-2.0

Tests, scripts, docs and project configuration are *not* checked, because the license that
applies to them is still open (LICENSE names only the runtime as the Licensed Work, while
LICENSING.md reads more broadly). Widen this script when that is resolved -- do not widen
it before, or the gate will assert a boundary the license text does not actually draw.

Two declaration forms are accepted, following REUSE:

1. A comment header in the file itself. The identifier must appear in an actual comment,
   not merely somewhere in the text, so that a docstring quoting an identifier cannot
   satisfy the gate.
2. An adjacent ``<filename>.license`` sidecar, for formats with no comment syntax, such
   as JSON and the PEP 561 ``py.typed`` marker.

Every regular file in a region must use one or the other. Whole-directory LICENSE files
are deliberately *not* accepted as a declaration: they do not travel with the file, which
is the entire failure this gate exists to prevent.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent

MARKER = "SPDX-License-Identifier:"
SIDECAR_SUFFIX = ".license"


class Region(NamedTuple):
    """A directory whose license is settled, and which must therefore be fully declared."""

    path: str
    license_id: str
    # The region's own license text. It is required to exist and is not itself an
    # annotated target. Named per region rather than exempted globally: only the
    # pure-data regions carry one, so nothing under src/ledgergate can hide behind a
    # reserved filename.
    license_text: str | None = None


REGIONS: list[Region] = [
    Region("src/ledgergate", "BUSL-1.1"),
    Region("corpus", "Apache-2.0", license_text="LICENSE"),
    Region("schema", "Apache-2.0", license_text="LICENSE"),
]

# Comment prefixes by extension. A file type absent from this map cannot carry an inline
# header and must use a sidecar.
COMMENT_PREFIXES: dict[str, tuple[str, ...]] = {
    ".py": ("#",),
    ".pyi": ("#",),
    ".yaml": ("#",),
    ".yml": ("#",),
    ".toml": ("#",),
    ".cfg": ("#",),
    ".sh": ("#",),
}

# Only build artefacts are skipped. Directory names such as LICENSES are *not* exempt
# here: nothing inside a shipped package should be unreadable to this gate.
EXEMPT_DIRS = {"__pycache__"}

# How far into a file the declaration may appear. Generous enough for a shebang, an
# encoding line and a module docstring; tight enough that it stays near the top.
HEADER_LINES = 20


def _display(path: Path) -> str:
    """Repo-relative path where possible, absolute otherwise (tmp dirs under test)."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _declared_identifier(text: str, prefixes: tuple[str, ...]) -> str | None:
    """Return the identifier declared in a comment header, or None if there is none.

    ``prefixes`` are the comment markers valid for the file type. An empty tuple means
    bare lines are accepted, which is the sidecar case.
    """
    for raw in text.splitlines()[:HEADER_LINES]:
        line = raw.strip()
        if prefixes:
            matched = next((p for p in prefixes if line.startswith(p)), None)
            if matched is None:
                continue
            line = line[len(matched) :].strip()
        if line.startswith(MARKER):
            return line[len(MARKER) :].strip()
    return None


def check_file(path: Path, expected: str) -> str | None:
    """Return a violation message, or None if the file declares the expected license.

    Checks the sidecar first: REUSE gives ``<file>.license`` precedence over the contents
    of the file it annotates.
    """
    name = _display(path)

    sidecar = path.with_name(path.name + SIDECAR_SUFFIX)
    if sidecar.is_file():
        found = _declared_identifier(sidecar.read_text(encoding="utf-8"), ("#",))
        if found is None:
            found = _declared_identifier(sidecar.read_text(encoding="utf-8"), ())
        if found == expected:
            return None
        if found is None:
            return f"{name}: sidecar {sidecar.name} declares no {MARKER}"
        return f"{name}: sidecar declares '{found}', expected '{expected}'"

    prefixes = COMMENT_PREFIXES.get(path.suffix)
    if prefixes is None:
        return (
            f"{name}: no comment syntax for '{path.suffix or path.name}';"
            f" add {path.name}{SIDECAR_SUFFIX} declaring {expected}"
        )

    found = _declared_identifier(path.read_text(encoding="utf-8"), prefixes)
    if found == expected:
        return None
    if found is None:
        return f"{name}: no '{MARKER} {expected}' comment in first {HEADER_LINES} lines"
    return f"{name}: declares '{found}', expected '{expected}'"


def _visible(path: Path) -> bool:
    """Whether this gate is allowed to see the file at all."""
    return path.is_file() and not EXEMPT_DIRS.intersection(path.parts)


def _targets(base: Path, license_text: str | None = None) -> list[Path]:
    """Every regular file in a region that must carry a declaration."""
    return sorted(
        p
        for p in base.rglob("*")
        if _visible(p)
        and p.suffix != SIDECAR_SUFFIX
        and not (license_text is not None and p.parent == base and p.name == license_text)
    )


def _orphan_sidecars(base: Path) -> list[Path]:
    """Sidecars that annotate nothing.

    ``.license`` is a reserved suffix that the target scan skips, so an orphan is a hole:
    arbitrary content could ship inside the package under that name without ever being
    read by this gate. An orphan is therefore a violation, not a curiosity.
    """
    return sorted(
        p
        for p in base.rglob(f"*{SIDECAR_SUFFIX}")
        if _visible(p)
        and p.name != SIDECAR_SUFFIX
        and not p.with_name(p.name[: -len(SIDECAR_SUFFIX)]).is_file()
    )


def main() -> int:
    """Scan every license-critical region. Returns the process exit code."""
    violations: list[str] = []
    checked = 0

    for region in REGIONS:
        base = ROOT / region.path
        # Fail closed. A region that vanished because of a rename is a gate that has
        # silently stopped protecting anything, which is the failure mode this whole
        # script exists to avoid.
        if not base.is_dir():
            violations.append(f"{region.path}: configured region is missing")
            continue
        # The exemption above is only sound while the file it exempts actually exists.
        # corpus/ and schema/ carry the whole Apache-2.0 grant in that one notice, so
        # losing it silently would leave the region configured as Apache while nothing
        # in the tree says so.
        if region.license_text is not None and not (base / region.license_text).is_file():
            violations.append(
                f"{region.path}: required license text {region.license_text} is missing"
            )
        for path in _targets(base, region.license_text):
            checked += 1
            if violation := check_file(path, region.license_id):
                violations.append(violation)
        for orphan in _orphan_sidecars(base):
            violations.append(f"{_display(orphan)}: sidecar annotates no such file")

    if violations:
        print("License header check FAILED. Every file in a mixed-license region")
        print("must name its own license, inline or via a .license sidecar.\n")
        for line in violations:
            print(f"  {line}")
        print(f"\n{len(violations)} violation(s) across {checked} file(s) checked.")
        return 1

    print(f"licenses: OK ({checked} file(s) carry a matching SPDX identifier)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
