# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Entry point for the ``ledgergate`` command."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

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

    sub = parser.add_subparsers(
        dest="command", metavar="{journal,approve,run,verify,record,report}"
    )
    for name, help_text in (
        ("run", "run an agent against the corpus and score it"),
        ("verify", "verify an existing trace against the corpus"),
        ("record", "record a cassette from a live agent run"),
        ("report", "render a result.json into markdown, junit or sarif"),
    ):
        sub.add_parser(name, help=help_text)

    journal = sub.add_parser("journal", help="inspect a journal file")
    journal_sub = journal.add_subparsers(dest="journal_command", metavar="{dump,pending}")
    dump = journal_sub.add_parser("dump", help="print every row of every table as JSON lines")
    dump.add_argument("path", help="path to the journal file")
    dump.add_argument(
        "--table",
        choices=[*FACT_TABLES, "journal"],
        help="restrict to one table (default: all, in journal order)",
    )
    dump.set_defaults(handler=journal_dump)
    pend = journal_sub.add_parser("pending", help="list operations awaiting approval")
    pend.add_argument("path", help="path to the journal file")
    pend.set_defaults(handler=journal_pending)

    approve = sub.add_parser(
        "approve", help="issue a signed approval artefact for one pending operation"
    )
    approve.add_argument("path", help="path to the journal file")
    approve.add_argument("--key", required=True, help="the pending operation's stored key")
    approve.add_argument("--approver", required=True, help="who approves (an identifier)")
    approve.add_argument("--approval-id", required=True, help="a fresh, unique identifier")
    approve.add_argument(
        "--signing-key",
        required=True,
        type=Path,
        help="file holding the 32-byte Ed25519 private key (raw or hex)",
    )
    approve.add_argument("--valid-hours", type=float, default=24.0)
    approve.add_argument(
        "--subject",
        default=None,
        help="display subject for the approver (the policy set defines what a subject is)",
    )
    approve.set_defaults(handler=journal_approve)

    return parser


def _pending_rows(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    return [
        (str(r[0]), str(r[1]), str(r[2]), str(r[3]))
        for r in conn.execute(
            "SELECT op.key, op.fingerprint, op.command, d.journal_id FROM operations op"
            " JOIN definition d"
            " WHERE (SELECT outcome FROM outcomes o WHERE o.operation = op.journal_sequence"
            "        ORDER BY o.journal_sequence DESC LIMIT 1) = 'awaiting_approval'"
            " ORDER BY op.journal_sequence"
        )
    ]


def _read_only(path: str) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def journal_pending(args: argparse.Namespace) -> int:
    try:
        conn = _read_only(args.path)
    except sqlite3.Error as exc:
        print(f"cannot read journal at {args.path}: {exc}", file=sys.stderr)
        return 2
    try:
        for key, fingerprint, command, _journal_id in _pending_rows(conn):
            print(
                json.dumps(
                    {"key": key, "fingerprint": fingerprint, "command": json.loads(command)},
                    sort_keys=True,
                )
            )
    except sqlite3.Error as exc:
        print(f"cannot read journal at {args.path}: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    return 0


def journal_approve(args: argparse.Namespace) -> int:
    """Issue an artefact bound to the named pending operation. The signing key never leaves
    this process; only the artefact is printed."""
    from datetime import UTC, datetime, timedelta

    from ledgergate.journal import issue, signing_key_from_bytes, verification_key_text
    from ledgergate.ledger import InvalidIdentifierError
    from ledgergate.ledger.identifiers import require_identifier

    try:
        require_identifier(args.approver, "--approver")
        require_identifier(args.approval_id, "--approval-id")
        if args.subject is not None:
            require_identifier(args.subject, "--subject")
    except InvalidIdentifierError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    raw = args.signing_key.read_bytes()
    try:
        # 32 raw bytes as written, or 64 hex characters (whitespace around the hex ignored;
        # raw bytes are never stripped, since a key byte may itself be whitespace).
        private = signing_key_from_bytes(
            raw if len(raw) == 32 else bytes.fromhex(raw.decode("ascii").strip())
        )
    except (ValueError, UnicodeDecodeError) as exc:
        print(f"signing key is not a 32-byte Ed25519 private key: {exc}", file=sys.stderr)
        return 2
    try:
        conn = _read_only(args.path)
    except sqlite3.Error as exc:
        print(f"cannot read journal at {args.path}: {exc}", file=sys.stderr)
        return 2
    try:
        match = [r for r in _pending_rows(conn) if r[0] == args.key]
        (approval_key,) = conn.execute("SELECT approval_key FROM definition").fetchone()
        used = conn.execute(
            "SELECT 1 FROM approval_consumptions WHERE approval_id = ?", (args.approval_id,)
        ).fetchone()
    finally:
        conn.close()
    if approval_key != verification_key_text(private):
        print("signing key does not match the journal's verification key", file=sys.stderr)
        return 1
    if used is not None:
        print(f"approval id {args.approval_id!r} has already been consumed", file=sys.stderr)
        return 1
    if not match:
        print(f"no operation with key {args.key!r} is awaiting approval", file=sys.stderr)
        return 1
    key, fingerprint, command_json, journal_id = match[0]
    command = json.loads(command_json)
    money = command.get("amount") or command.get("money")
    now = datetime.now(UTC)
    artefact = issue(
        private,
        journal_id=journal_id,
        approval_id=args.approval_id,
        approver=args.approver,
        fingerprint=fingerprint,
        key=key,
        issued_at=now,
        expires_at=now + timedelta(hours=args.valid_hours),
        subject=args.subject,
        amount=None if money is None else str(money["amount"]),
        currency=None if money is None else money["currency"],
    )
    print(json.dumps(artefact.to_json(), sort_keys=True))
    return 0


def journal_dump(args: argparse.Namespace) -> int:
    """Rows in ``journal_sequence`` order, one JSON object per line, read-only."""
    try:
        conn = sqlite3.connect(Path(args.path).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
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
    except sqlite3.Error as exc:
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
