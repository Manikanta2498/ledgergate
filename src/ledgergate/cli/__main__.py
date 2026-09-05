# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Entry point for the ``ledgergate`` command."""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ledgergate import __version__
from ledgergate.journal import FACT_TABLES, SCHEMA_VERSION


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="ledgergate",
        description="Prove an agent respects financial state machines before deployment.",
    )
    parser.add_argument("--version", action="version", version=f"ledgergate {__version__}")
    parser.set_defaults(handler=None)

    sub = parser.add_subparsers(
        dest="command", metavar="{journal,approve,serve,run,verify,record,report}"
    )
    run = sub.add_parser("run", help="score scripted or supplied traces against the corpus")
    run.add_argument(
        "--corpus", required=True, type=Path, help="corpus root (scenarios/, expectations/)"
    )
    run.add_argument("--traces", type=Path, help="directory of <id>.json traces to score")
    run.add_argument("--out", type=Path, help="write result.json here; default stdout")
    run.add_argument("--only", action="append", default=[], metavar="ID")
    run.add_argument("--kind", choices=["correct", "red-team"])
    run.add_argument("--keep-traces", type=Path, help="write the traces the runner produced")
    run.add_argument(
        "--emit-setup",
        nargs=2,
        metavar=("ID", "PATH"),
        help="create the scenario's journal (and PATH.policy.json) for a live agent",
    )
    run.set_defaults(handler=run_command)

    report = sub.add_parser("report", help="render a result.json, or a drift table over two")
    report.add_argument("results", nargs="*", type=Path, help="result.json (two with --drift)")
    report.add_argument("--format", choices=["md", "junit", "sarif", "json"], default="md")
    report.add_argument("--drift", action="store_true", help="compare baseline and candidate")
    report.add_argument("--allow-newly-skipped", action="store_true")
    report.add_argument("--out", type=Path)
    report.set_defaults(handler=report_command)

    record = sub.add_parser(
        "record",
        help="convert an OpenTelemetry GenAI export into a v1 trace (docs/spec/otel-adapter.md)",
    )
    record.add_argument(
        "--from-otel", required=True, type=Path, metavar="EXPORT", help="OTLP/JSON export file"
    )
    record.add_argument(
        "--out", type=Path, help="write the trace here (atomically); default stdout"
    )
    record.set_defaults(handler=record_command)

    verify = sub.add_parser(
        "verify",
        help="check a trace (v1 or v2 JSON) or a journal file against every invariant",
    )
    verify.add_argument("source", help="path to a trace document or a journal file")
    verify.add_argument("--json", action="store_true", help="print the scorecard as JSON")
    verify.add_argument(
        "--emit-trace",
        type=Path,
        default=None,
        help="also write the v2 trace that was checked (derived from a journal, or lifted)",
    )
    verify.set_defaults(handler=verify_command)

    serve = sub.add_parser(
        "serve", help="serve one journal as MCP tools over stdio (docs/spec/mcp-runtime.md)"
    )
    serve.add_argument("--journal", required=True, type=Path, help="path to the journal file")
    serve.add_argument("--create", action="store_true", help="create the journal from --chart")
    serve.add_argument("--chart", type=Path, help="JSON array of accounts (AccountDoc shape)")
    serve.add_argument("--policy", type=Path, help="ThresholdPolicySet configuration document")
    serve.add_argument("--approval-key", help="Ed25519 verification key text; with --create only")
    serve.add_argument("--token-key-file", type=Path, help="tokenizer key, 32+ raw bytes")
    serve.add_argument("--principal", default="local", help="the one local principal")
    serve.set_defaults(handler=serve_command)

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
    except InvalidIdentifierError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        raw = args.signing_key.read_bytes()
    except OSError as exc:
        print(f"cannot read signing key: {exc}", file=sys.stderr)
        return 2
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
        (schema_version,) = conn.execute("SELECT schema_version FROM definition").fetchone()
        if schema_version != SCHEMA_VERSION:
            print(
                f"journal is schema {schema_version}; this build is {SCHEMA_VERSION}",
                file=sys.stderr,
            )
            return 2
        match = [r for r in _pending_rows(conn) if r[0] == args.key]
        (approval_key,) = conn.execute("SELECT approval_key FROM definition").fetchone()
        used = conn.execute(
            "SELECT 1 FROM approval_consumptions WHERE approval_id = ?", (args.approval_id,)
        ).fetchone()
    except sqlite3.Error as exc:
        print(f"cannot read journal at {args.path}: {exc}", file=sys.stderr)
        return 2
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
        subject=command.get("transaction_id"),  # the stored token, never operator-typed
        amount=None if money is None else str(money["amount"]),
        currency=None if money is None else money["currency"],
    )
    print(json.dumps(artefact.to_json(), sort_keys=True))
    return 0


def _emit(text: str, out: Path | None) -> int:
    if out is None:
        sys.stdout.write(text)
    else:
        out.write_text(text, encoding="utf-8")
    return 0


def run_command(args: argparse.Namespace) -> int:
    """Spec corpus.md: 0 all scored passed, 1 any fail/error, 2 corpus fault, 3 nothing scored."""
    from ledgergate.report import dump_result
    from ledgergate.runner import CorpusError, emit_setup, load_corpus, run

    try:
        corpus = load_corpus(args.corpus)
        if args.emit_setup is not None:
            scenario_id, path = args.emit_setup
            matches = [s for s in corpus.scenarios if s.id == scenario_id]
            if not matches:
                raise CorpusError(f"no scenario {scenario_id!r}")
            if args.traces or args.only or args.kind or args.out or args.keep_traces:
                raise CorpusError("--emit-setup takes no other options")
            emit_setup(matches[0], Path(path))
            if matches[0].setup.approvals is not None:
                print(
                    "ledgergate run: journal created for scoring only; the corpus signing key"
                    " is public data, so anyone can approve against it",
                    file=sys.stderr,
                )
            return 0
        result = run(
            corpus,
            traces=args.traces,
            only=tuple(args.only),
            kind=args.kind,
            keep_traces=args.keep_traces,
        )
    except CorpusError as exc:
        print(f"ledgergate run: corpus fault: {exc}", file=sys.stderr)
        return 2
    _emit(dump_result(result), args.out)
    return result.gate


def report_command(args: argparse.Namespace) -> int:
    from ledgergate.report import (
        ResultError,
        drift,
        load_result,
        render_drift_json,
        render_drift_markdown,
        render_junit,
        render_markdown,
        render_sarif,
    )

    def fail(message: str) -> int:
        print(f"ledgergate report: {message}", file=sys.stderr)
        return 2

    try:
        docs = [load_result(p.read_text(encoding="utf-8")) for p in args.results]
    except (OSError, ResultError) as exc:
        return fail(f"cannot read result: {type(exc).__name__}: {exc}")
    if args.drift:
        if len(docs) != 2:
            return fail("--drift needs a baseline and a candidate")
        try:
            table = drift(docs[0], docs[1], allow_newly_skipped=args.allow_newly_skipped)
        except ResultError as exc:
            return fail(str(exc))
        text = render_drift_json(table) if args.format == "json" else render_drift_markdown(table)
        _emit(text, args.out)
        return table.gate
    if len(docs) != 1:
        return fail("one result.json, or two with --drift")
    renderers = {"md": render_markdown, "junit": render_junit, "sarif": render_sarif}
    if args.format not in renderers:
        return fail("--format json is for --drift only")
    return _emit(renderers[args.format](docs[0]), args.out)


def record_command(args: argparse.Namespace) -> int:
    """Spec otel-adapter.md *CLI*: 0 trace, 1 report, 2 unreadable, 70 self-check failure."""
    import os
    import tempfile

    from ledgergate.adapters.otel import (
        MAX_FILE_BYTES,
        SelfCheckError,
        UnreadableError,
        convert,
        self_check,
    )
    from ledgergate.trace.io import dump_trace

    try:
        with args.from_otel.open("rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)  # read with a limit: the bound, not the file
    except OSError as exc:
        print(f"ledgergate record: cannot read: {type(exc).__name__}", file=sys.stderr)
        return 2
    try:
        outcome = convert(data)
    except UnreadableError as exc:
        hint = f" ({exc.hint})" if exc.hint else ""
        print(f"ledgergate record: cannot read: {exc}{hint}", file=sys.stderr)
        return 2
    if outcome.report is not None:
        print(outcome.report.render(), file=sys.stderr)
        return 1
    assert outcome.trace is not None
    try:
        trace = self_check(outcome.trace)
    except SelfCheckError as exc:
        print("ledgergate record: self-check failed (a bug):", file=sys.stderr)
        for problem in exc.problems:
            print(f"  {problem}", file=sys.stderr)
        return 70
    text = dump_trace(trace)
    if args.out is None:
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except OSError as exc:
            # the interpreter flushes stdout again at exit and would override the status
            # with 120 on a broken pipe: point fd 1 at /dev/null first
            with contextlib.suppress(OSError):
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            print(f"ledgergate record: cannot write: {type(exc).__name__}", file=sys.stderr)
            return 2
        return 0
    target: Path = args.out
    tmp: str | None = None
    try:
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        Path(tmp).replace(target)
    except OSError as exc:
        if tmp is not None:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()
        print(f"ledgergate record: cannot write --out: {type(exc).__name__}", file=sys.stderr)
        return 2
    return 0


def serve_command(args: argparse.Namespace) -> int:
    """Build the journal from the flags per mcp-runtime.md *Configuration and effects*, then
    hand it to the transport. The chart file is parsed here (the cli may import trace); mcp
    never does."""
    from ledgergate.journal import (
        ConfigurationError,
        IdentityAdmitter,
        Journal,
        JournalError,
        NullPolicySet,
        ThresholdPolicySet,
        TokenizingAdmitter,
    )
    from ledgergate.ledger import InvalidIdentifierError, LedgerError
    from ledgergate.ledger.identifiers import require_identifier
    from ledgergate.mcp import RandomIds, SystemClock, serve

    def fail(message: str) -> int:
        print(f"ledgergate serve: {message}", file=sys.stderr)
        return 2

    try:
        require_identifier(args.principal, "--principal")
    except InvalidIdentifierError as exc:
        return fail(str(exc))
    if args.approval_key is not None and not args.create:
        return fail("--approval-key is meaningful only with --create")
    if args.approval_key is not None:
        from ledgergate.journal import verification_key

        try:
            verification_key(args.approval_key)
        except ValueError as exc:
            return fail(f"--approval-key is not an Ed25519 verification key: {type(exc).__name__}")
    if args.create != (args.chart is not None):
        return fail("--create and --chart go together")

    policy: Any = NullPolicySet()
    if args.policy is not None:
        try:
            doc = json.loads(args.policy.read_text())
            if not isinstance(doc, dict):
                raise TypeError("configuration must be an object")
            policy = ThresholdPolicySet.from_configuration(doc)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            return fail(f"cannot load --policy: {type(exc).__name__}")
    admitter: Any = IdentityAdmitter()
    if args.token_key_file is not None:
        from ledgergate.codec import Tokenizer

        try:
            key = args.token_key_file.read_bytes()
        except OSError as exc:
            return fail(f"cannot read --token-key-file: {type(exc).__name__}")
        if len(key) < 32:
            return fail("--token-key-file must hold at least 32 bytes")
        admitter = TokenizingAdmitter(Tokenizer(key, domain="mcp", key_version="v1"))
    else:
        print(
            "ledgergate serve: warning: no --token-key-file; identifiers and free text reach"
            " disk as given",
            file=sys.stderr,
        )

    try:
        if args.create:
            from pydantic import TypeAdapter

            from ledgergate.ledger import CURRENCIES, ChartOfAccounts
            from ledgergate.trace.models import AccountDoc

            docs = TypeAdapter(list[AccountDoc]).validate_json(args.chart.read_text())
            chart = ChartOfAccounts(a.to_account(CURRENCIES) for a in docs)
            journal = Journal.create(
                str(args.journal),
                chart,
                clock=SystemClock(),
                ids=RandomIds(),
                admitter=admitter,
                policy=policy,
                principal=args.principal,
                approval_key=args.approval_key or "none",
            )
        else:
            journal = Journal.open(
                str(args.journal),
                clock=SystemClock(),
                ids=RandomIds(),
                admitter=admitter,
                policy=policy,
                principal=args.principal,
            )
    except (JournalError, ConfigurationError) as exc:
        return fail(f"cannot open journal: {type(exc).__name__}")
    except (OSError, ValueError, LookupError, LedgerError) as exc:
        return fail(f"cannot read --chart: {type(exc).__name__}")
    return serve(journal)


def verify_command(args: argparse.Namespace) -> int:
    """Exit 0 when at least one invariant ran and none failed, 1 when any failed, 2 when the
    source cannot be read, 3 when nothing could be checked (``no_evidence``: a trace that
    carries nothing any invariant quantifies over is never a pass)."""
    from ledgergate.derive import DerivationError
    from ledgergate.derive import trace as derive_trace
    from ledgergate.invariants import check
    from ledgergate.trace import TraceError, dump_v2, load_any

    source = Path(args.source)
    try:
        with source.open("rb") as fh:
            header = fh.read(16)
    except OSError as exc:
        print(f"cannot read {source}: {exc}", file=sys.stderr)
        return 2
    try:
        if header.startswith(b"SQLite format 3"):
            trace = derive_trace(str(source))
        else:
            trace = load_any(source)
    except (DerivationError, TraceError, sqlite3.Error, OSError, ValueError, KeyError) as exc:
        # pydantic's ValidationError is a ValueError: a journal whose rows the grammar cannot
        # express is reported, not tracebacked
        print(f"cannot verify {source}: {exc}", file=sys.stderr)
        return 2
    try:
        card = check(trace)
    except Exception as exc:  # an invariant that raises is a bug in the registry, not a verdict
        print(
            f"cannot verify {source}: invariant raised {type(exc).__name__}: {exc}", file=sys.stderr
        )
        return 2
    if args.emit_trace is not None:
        args.emit_trace.write_text(dump_v2(trace), encoding="utf-8")
    if args.json:
        print(json.dumps(card.as_json(), indent=2, sort_keys=True))
    else:
        for r in card.results:
            print(f"{r.status:<12} {r.name}")
            for f in r.findings:
                where = f" [{f.intent_id}]" if f.intent_id else ""
                print(f"             {f.severity}{where}: {f.message}")
        print(
            f"{card.status.upper()}: {card.intents} intents, {card.ledger_commands} ledger commands"
        )
    return {"pass": 0, "fail": 1, "no_evidence": 3}[card.status]


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

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
