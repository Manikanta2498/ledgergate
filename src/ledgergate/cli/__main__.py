# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Entry point for the ``ledgergate`` command."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence

from ledgergate import __version__
from ledgergate.journal import FACT_TABLES


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="ledgergate",
        description="Prove an agent respects financial state machines before deployment.",
    )
    parser.add_argument("--version", action="version", version=f"ledgergate {__version__}")
    parser.set_defaults(handler=None)

    sub = parser.add_subparsers(dest="command", metavar="{journal,run,verify,record,report}")
    for name, help_text in (
        ("run", "run an agent against the corpus and score it"),
        ("verify", "verify an existing trace against the corpus"),
        ("record", "record a cassette from a live agent run"),
        ("report", "render a result.json into markdown, junit or sarif"),
    ):
        sub.add_parser(name, help=help_text)

    journal = sub.add_parser("journal", help="inspect a journal file")
    journal_sub = journal.add_subparsers(dest="journal_command", metavar="{dump}")
    dump = journal_sub.add_parser("dump", help="print every row of every table as JSON lines")
    dump.add_argument("path", help="path to the journal file")
    dump.add_argument(
        "--table",
        choices=[*FACT_TABLES, "journal"],
        help="restrict to one table (default: all, in journal order)",
    )
    dump.set_defaults(handler=journal_dump)

    return parser


def journal_dump(args: argparse.Namespace) -> int:
    """Rows in ``journal_sequence`` order, one JSON object per line, read-only."""
    try:
        conn = sqlite3.connect(f"file:{args.path}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        print(f"cannot read journal at {args.path}: {exc}", file=sys.stderr)
        return 2
    try:
        tables = [args.table] if args.table else ["journal", *FACT_TABLES]
        rows: list[tuple[int, str, dict[str, object]]] = []
        for table in tables:
            cur = conn.execute(f"SELECT * FROM {table}")  # noqa: S608 - names from FACT_TABLES
            names = [d[0] for d in cur.description]
            rows.extend((int(r[0]), table, dict(zip(names, r, strict=True))) for r in cur)
        for _seq, table, row in sorted(rows, key=lambda r: (r[0], r[1] != "journal")):
            print(json.dumps({"table": table, **row}, sort_keys=True, ensure_ascii=False))
    except sqlite3.OperationalError as exc:
        print(f"cannot read journal at {args.path}: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0
    if args.handler is not None:
        return int(args.handler(args))
    if args.command == "journal":
        parser.parse_args(["journal", "--help"])
        return 0

    print(f"'{args.command}' is not implemented yet (milestone M3).", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
