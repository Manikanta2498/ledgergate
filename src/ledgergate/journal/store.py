# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""The journal: a strictly append-only SQLite record of every attempt to move money.

This module implements the write and read protocols of ``docs/spec/journal.md`` step for
step. The in-memory :class:`~ledgergate.ledger.Ledger` is a projection rebuilt from
``outcomes``; the journal is the only durable truth. One invocation is one
``BEGIN IMMEDIATE`` transaction, the response is rendered only after commit, and every
row is written after every row it references, so the protocol's order is the only order
the foreign keys accept.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ledgergate.codec import (
    CODEC_VERSION,
    IJsonError,
    canonical_text,
    decode_command,
    digest,
    encode_command,
    require_ijson,
)
from ledgergate.journal.admission import (
    TOOLS,
    AdmissionError,
    AdmissionScope,
    Admitter,
    IdentityAdmitter,
    Request,
)
from ledgergate.journal.policy import NullPolicySet, PolicyContext, PolicySet
from ledgergate.journal.schema import SCHEMA_VERSION, connect, create_schema
from ledgergate.ledger import (
    CURRENCIES,
    Account,
    AccountType,
    Applied,
    ChartOfAccounts,
    Clock,
    Currency,
    IdGenerator,
    InvalidIdentifierError,
    Ledger,
    LedgerError,
    Money,
    command_fingerprint,
)
from ledgergate.ledger.identifiers import require_identifier

LOCAL_PRINCIPAL = "local"
ENVELOPE_BOUND = 4096  # bytes of UTF-8, per the specification


class JournalError(Exception):
    """A failure the journal cannot record: the transaction is rolled back and nothing is
    written. Stated rather than hidden; see the specification's failure list."""


class IntegrityError(JournalError):
    """The journal's own consistency check failed during rebuild."""


class ConfigurationError(JournalError):
    """The journal and this process disagree about a version, or the policy set behaved in
    a way the protocol forbids."""


@dataclass(frozen=True, slots=True)
class Response:
    """What the caller receives, rendered from committed rows only."""

    invocation: int
    disposition: str
    response: str
    ok: bool
    result: Any = None
    error_type: str | None = None
    error_message: str | None = None
    outcome: int | None = None

    def as_tool_result(self) -> dict[str, Any]:
        if self.ok:
            return {"ok": True, "result": self.result}
        return {"ok": False, "error": {"type": self.error_type, "message": self.error_message}}


@dataclass(frozen=True, slots=True)
class Definition:
    journal_id: str
    chart: ChartOfAccounts
    currencies: Mapping[str, Currency]
    codec_version: str = CODEC_VERSION
    policy_set_version: str = "none"
    token_domain: str = "none"  # noqa: S105 - a domain label, not a credential
    token_key_version: str = "none"  # noqa: S105 - a version label, not a credential
    approval_key: str = "none"

    @property
    def registry(self) -> dict[str, Currency]:
        out = dict(CURRENCIES)
        out.update(self.currencies)
        out.update(self.chart.currencies())
        return out


def _money_str(m: Money) -> dict[str, str]:
    return {"amount": str(m.amount), "currency": m.currency.code}


class EffectError(JournalError):
    """The process's injected clock or id generator produced something the core would
    refuse. That is a fault of this process, not a verdict on the command, so it is never
    recorded as a rejection and never spends the caller's key."""


class _Effects:
    """Feeds recorded effects back to the core on rebuild; on live execution, validates what
    the injected effects produce before the core sees it."""

    def __init__(self, clock: Clock, ids: IdGenerator, ledger: Ledger) -> None:
        self._clock, self._ids, self._ledger = clock, ids, ledger
        self._scripted: tuple[str, datetime] | None = None

    @staticmethod
    def aware_now(clock: Clock) -> datetime:
        """The clock's reading, in UTC, or an :class:`EffectError` if it is naive.
        ``astimezone`` on a naive value would consult the host's zone: a hidden input."""
        at = clock.now()
        if at.tzinfo is None or at.utcoffset() is None:
            raise EffectError("clock produced a naive datetime")
        return at.astimezone(UTC)

    def script(self, entry_id: str, posted_at: datetime) -> None:
        self._scripted = (entry_id, posted_at)

    def next_id(self) -> str:
        if self._scripted:
            return self._scripted[0]
        entry_id = self._ids.next_id()
        try:
            require_identifier(entry_id, "generated entry id")
        except InvalidIdentifierError as exc:
            raise EffectError(f"id generator produced an invalid id: {exc}") from exc
        if any(e.entry_id == entry_id for e in self._ledger.entries):
            raise EffectError(
                f"id generator produced {entry_id!r}, which this ledger already holds;"
                " generators must be fresh across processes"
            )
        return entry_id

    def now(self) -> datetime:
        if self._scripted:
            at = self._scripted[1]
            self._scripted = None
            return at
        return self.aware_now(self._clock)


@dataclass
class Journal:
    """Open with :meth:`create` or :meth:`open`; drive with :meth:`handle`."""

    path: str
    clock: Clock
    ids: IdGenerator
    admitter: Admitter = field(default_factory=IdentityAdmitter)
    policy: PolicySet = field(default_factory=NullPolicySet)
    principal: str = LOCAL_PRINCIPAL
    _conn: Any = field(init=False, repr=False)
    _definition: Definition = field(init=False, repr=False)
    _ledger: Ledger = field(init=False, repr=False)
    _cursor: int = field(init=False, default=0)
    _pending_projection: tuple[Ledger, int] | None = field(init=False, default=None, repr=False)

    # ------------------------------------------------------------- lifecycle

    @classmethod
    def create(
        cls,
        path: str,
        chart: ChartOfAccounts,
        *,
        clock: Clock,
        ids: IdGenerator,
        currencies: Mapping[str, Currency] | None = None,
        admitter: Admitter | None = None,
        policy: PolicySet | None = None,
    ) -> Journal:
        self = cls(path, clock, ids, admitter or IdentityAdmitter(), policy or NullPolicySet())
        self._conn = connect(path)
        create_schema(self._conn)
        if self._conn.execute("SELECT 1 FROM definition").fetchone():
            self._conn.close()
            raise JournalError("journal already has a definition; use open()")
        try:
            return cls._define(self, chart, currencies)
        except (JournalError, sqlite3.Error):
            self._conn.close()
            raise

    @classmethod
    def _define(
        cls, self: Journal, chart: ChartOfAccounts, currencies: Mapping[str, Currency] | None
    ) -> Journal:
        definition = Definition(
            journal_id=secrets.token_hex(16),
            chart=chart,
            currencies=dict(currencies or {}),
            policy_set_version=self.policy.version,
            token_domain=self.admitter.token_domain,
            token_key_version=self.admitter.token_key_version,
        )
        registry = definition.registry
        with self._txn():
            seq = self._alloc("definition")
            self._conn.execute(
                "INSERT INTO definition VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    seq,
                    1,
                    definition.journal_id,
                    SCHEMA_VERSION,
                    definition.codec_version,
                    definition.policy_set_version,
                    definition.token_domain,
                    definition.token_key_version,
                    definition.approval_key,
                    json.dumps(_encode_chart(chart, self.admitter), sort_keys=True),
                    json.dumps({c: cur.exponent for c, cur in registry.items()}, sort_keys=True),
                    _Effects.aware_now(self.clock).isoformat(),
                ),
            )
        self._definition = definition
        self._ledger = Ledger.empty(chart)
        self._cursor = 0
        return self

    @classmethod
    def open(
        cls,
        path: str,
        *,
        clock: Clock,
        ids: IdGenerator,
        admitter: Admitter | None = None,
        policy: PolicySet | None = None,
    ) -> Journal:
        self = cls(path, clock, ids, admitter or IdentityAdmitter(), policy or NullPolicySet())
        try:
            self._conn = connect(path, create=False)
        except sqlite3.Error as exc:
            raise JournalError(f"cannot open journal at {path}: {exc}") from exc
        try:
            # Read the definition before touching the file: a journal from another schema
            # version must be refused, not upgraded in place.
            try:
                row = self._conn.execute(
                    "SELECT journal_id, codec_version, policy_set_version, token_domain,"
                    " token_key_version, approval_key, chart, currencies, schema_version"
                    " FROM definition"
                ).fetchone()
            except sqlite3.Error as exc:
                raise JournalError(f"not a journal: {exc}") from exc
            if row is None:
                raise JournalError("no definition; use create()")
            if row[8] != SCHEMA_VERSION or row[1] != CODEC_VERSION:
                raise ConfigurationError(
                    f"journal is schema {row[8]}/codec {row[1]!r};"
                    f" this process is schema {SCHEMA_VERSION}/codec {CODEC_VERSION!r}"
                )
            if row[2] != self.policy.version:
                raise ConfigurationError(
                    f"journal was defined with policy set {row[2]!r};"
                    f" this process runs {self.policy.version!r}"
                )
            if (row[3], row[4]) != (self.admitter.token_domain, self.admitter.token_key_version):
                raise ConfigurationError(
                    f"journal tokens are {row[3]!r}/{row[4]!r}; this admitter is"
                    f" {self.admitter.token_domain!r}/{self.admitter.token_key_version!r}"
                )
            currencies = {code: Currency(code, exp) for code, exp in json.loads(row[7]).items()}
            chart = _decode_chart(json.loads(row[6]), currencies)
            self._definition = Definition(
                row[0], chart, currencies, row[1], row[2], row[3], row[4], row[5]
            )
            self._ledger = Ledger.empty(chart)
            self._cursor = 0
            self._conn.execute("BEGIN")  # one snapshot for the chain check and the fold
            try:
                self._rebuild()
            finally:
                self._conn.execute("COMMIT")
        except (JournalError, sqlite3.Error):
            self._conn.close()
            raise
        return self

    def close(self) -> None:
        self._conn.close()

    @property
    def definition(self) -> Definition:
        return self._definition

    @property
    def ledger(self) -> Ledger:
        """The projection. Never authoritative; rebuilt from outcomes."""
        return self._ledger

    @property
    def cursor(self) -> int:
        return self._cursor

    # -------------------------------------------------------------- protocol

    def handle(self, value: Any) -> Response:
        """One invocation, one transaction. ``value`` is an already-decoded I-JSON value.

        A value that is not I-JSON cannot be digested faithfully; that is the transport's
        contract, so it is refused before any row is written and raised as
        :class:`JournalError`, the stated unrecorded-failure class.
        """
        try:
            require_ijson(value)
        except IJsonError as exc:
            raise JournalError(f"input is not I-JSON: {exc}") from exc
        scope = AdmissionScope(self._definition.registry, self._definition.chart, self.principal)
        with self._txn():
            self._ensure_current()  # step 2
            try:
                request = self.admitter.admit(value, scope)  # step 3
            except AdmissionError as exc:
                return self._invalid(value, exc)
            if request.is_read:
                return self._read(request)
            return self._write(request)

    def record_message(self, role: str, content: str) -> int:
        """A standalone message event: its own transaction, no invocation."""
        with self._txn():
            seq = self._alloc("events")
            self._conn.execute(
                "INSERT INTO events VALUES (?,?,?,?)",
                (
                    seq,
                    None,
                    "message",
                    json.dumps(
                        {"role": role, "content": self.admitter.redact_text(content)},
                        sort_keys=True,
                    ),
                ),
            )
            return seq

    # ---------------------------------------------------------------- write

    def _write(self, request: Request) -> Response:
        assert request.command is not None and request.key is not None
        command = request.command
        fingerprint = command_fingerprint(command)
        now = _Effects.aware_now(self.clock)
        encoded = json.dumps(encode_command(command), sort_keys=True)
        row = self._conn.execute(
            "SELECT journal_sequence, fingerprint FROM operations WHERE key = ?", (request.key,)
        ).fetchone()

        # step 4: resolve the key and write the invocation
        if row is None:
            op_seq = self._alloc("operations")
            self._conn.execute(
                "INSERT INTO operations VALUES (?,?,?,?)",
                (op_seq, request.key, fingerprint, encoded),
            )
            disposition = "new"
        elif row[1] == fingerprint:
            op_seq, disposition = row[0], "replay"  # approvals are refused at admission in M2b
        else:
            op_seq, disposition = row[0], "conflict"

        inv_seq = self._alloc("invocations")
        self._conn.execute(
            "INSERT INTO invocations VALUES (?,?,?,?,?,?,?,?,?)",
            (
                inv_seq,
                op_seq,
                now.isoformat(),
                self.principal,
                disposition,
                fingerprint,
                encoded,
                request.request_digest(),
                request.call_id,
            ),
        )
        self._inbound(inv_seq, request)  # step 5

        # step 6: short paths
        if disposition == "replay":
            outcome_seq = self._current_outcome(op_seq)
            if outcome_seq is None:  # pragma: no cover - invariant 2 forbids this
                raise IntegrityError(f"operation {op_seq} has no outcome")
            response = self._render_replay(inv_seq, outcome_seq)
            self._respond(inv_seq, disposition, outcome_seq, "replayed", response)
            return response
        if disposition == "conflict":
            response = Response(
                inv_seq,
                disposition,
                "conflict",
                False,
                error_type="IdempotencyConflictError",
                error_message=f"key {request.key!r} was used for a different request",
            )
            self._respond(inv_seq, disposition, None, "conflict", response)
            return response

        # step 7: decide
        context = PolicyContext(
            principal=self.principal,
            subject=None,
            command_digest=fingerprint,
            digest_kind="fingerprint",
            evaluated_at=now,
            policy_set_version=self.policy.version,
        )
        decision = self.policy.evaluate(context)
        dec_seq = self._decision(inv_seq, op_seq, context, decision)
        head = self._ledger.head
        if decision.decision != "allow":
            kind = "denied" if decision.decision == "deny" else "awaiting_approval"
            outcome_seq = self._outcome(op_seq, kind, dec_seq, head, head)
            response = Response(
                inv_seq,
                disposition,
                kind,
                False,
                error_type="PolicyDenied" if kind == "denied" else "ApprovalRequired",
                error_message=f"{decision.matched_rule}: {decision.reason}",
                outcome=outcome_seq,
            )
            self._respond(inv_seq, disposition, outcome_seq, kind, response)
            return response

        # step 8: execute through the pure core. A LedgerError here is the core's verdict on
        # the command against this projection; a fault of this process's effects raises
        # EffectError from _Effects first and is never recorded.
        effects = _Effects(self.clock, self.ids, self._ledger)
        try:
            applied = self._ledger.execute(command, clock=effects, ids=effects)
        except LedgerError as exc:
            message = self.admitter.redact_text(str(exc))
            outcome_seq = self._outcome(
                op_seq, "rejected", dec_seq, head, head, error=(type(exc).__name__, message)
            )
            response = Response(
                inv_seq,
                disposition,
                "rejected",
                False,
                error_type=type(exc).__name__,
                error_message=message,
                outcome=outcome_seq,
            )
            self._respond(inv_seq, disposition, outcome_seq, "rejected", response)
            return response

        # step 9: append the applied outcome
        entry = applied.entry
        outcome_seq = self._outcome(
            op_seq,
            "applied",
            dec_seq,
            head,
            applied.ledger.head,
            entry_id=None if entry is None else entry.entry_id,
            posted_at=None if entry is None else entry.posted_at,
            ledger_sequence=applied.ledger.sequence,
        )
        response = Response(
            inv_seq,
            disposition,
            "applied",
            True,
            result=_applied_result(applied),
            outcome=outcome_seq,
        )
        self._respond(inv_seq, disposition, outcome_seq, "applied", response)
        self._pending_projection = (applied.ledger, outcome_seq)  # advances on commit
        return response

    # ----------------------------------------------------------------- read

    def _read(self, request: Request) -> Response:
        now = _Effects.aware_now(self.clock)
        inv_seq = self._alloc("invocations")
        self._conn.execute(
            "INSERT INTO invocations VALUES (?,?,?,?,?,?,?,?,?)",
            (
                inv_seq,
                None,
                now.isoformat(),
                self.principal,
                "read",
                None,
                None,
                request.request_digest(),
                request.call_id,
            ),
        )
        self._inbound(inv_seq, request)
        if self.policy.gates_read(request.tool):
            context = PolicyContext(
                self.principal, None, request.request_digest(), "request", now, self.policy.version
            )
            decision = self.policy.evaluate(context)
            self._decision(inv_seq, None, context, decision)
            if decision.decision != "allow":
                response = Response(
                    inv_seq,
                    "read",
                    "denied",
                    False,
                    error_type="PolicyDenied",
                    error_message=f"{decision.matched_rule}: {decision.reason}",
                )
                self._respond(inv_seq, "read", None, "denied", response)
                return response
        result = self._serve(request)  # arguments were validated at admission
        rd_seq = self._alloc("reads")
        self._conn.execute(
            "INSERT INTO reads VALUES (?,?,?,?,?)",
            (rd_seq, inv_seq, self._cursor, self._ledger.head, digest(result)),
        )
        response = Response(inv_seq, "read", "read", True, result=result)
        self._respond(inv_seq, "read", None, "read", response)
        return response

    def _serve(self, request: Request) -> dict[str, Any]:
        if request.tool == "balance":
            account = request.arguments["account"]
            return {
                "account": account,
                "balance": _money_str(self._ledger.balance(account)),
                "cursor": self._cursor,
            }
        tb = self._ledger.trial_balance()
        return {
            "rows": [
                {
                    "account": r.account.account_id,
                    "debit": _money_str(r.debit),
                    "credit": _money_str(r.credit),
                }
                for r in tb.rows
            ],
            "balanced": tb.is_balanced,
            "cursor": self._cursor,
        }

    # -------------------------------------------------------------- invalid

    def _invalid(self, value: Any, exc: AdmissionError) -> Response:
        """Step 3 failure: an invocation with no operation and a bounded failure envelope
        in place of the request as received."""
        now = _Effects.aware_now(self.clock)
        call_id = value.get("call_id") if isinstance(value, dict) else None
        tool = value.get("tool") if isinstance(value, dict) else None
        safe_call_id = (
            self.admitter.tokenize_identifier(call_id)
            if isinstance(call_id, str) and _is_identifier(call_id)
            else None
        )
        inv_seq = self._alloc("invocations")
        self._conn.execute(
            "INSERT INTO invocations VALUES (?,?,?,?,?,?,?,?,?)",
            (
                inv_seq,
                None,
                now.isoformat(),
                self.principal,
                "invalid",
                None,
                None,
                None,
                safe_call_id,
            ),
        )
        envelope = {
            "call_id": safe_call_id,
            "tool": tool if isinstance(tool, str) and tool in TOOLS else None,
            "input_digest": digest(value),
            "error": {"code": exc.code, "path": exc.path or "$"},
            # The redactor runs over the whole serialized input as an untyped blob (the
            # identity admitter returns it unchanged); the byte bound applies after it.
            "payload": _bounded_utf8(
                self.admitter.redact_text(json.dumps(value, sort_keys=True, ensure_ascii=False))
            ),
        }
        self._event(inv_seq, "inbound", envelope)
        response = Response(
            inv_seq,
            "invalid",
            "invalid",
            False,
            error_type="AdmissionError",
            error_message=f"{exc.code} at {exc.path or '$'}",
        )
        self._respond(inv_seq, "invalid", None, "invalid", response)
        return response

    # -------------------------------------------------------------- rebuild

    def _ensure_current(self) -> None:
        (latest,) = self._conn.execute(
            "SELECT COALESCE(MAX(journal_sequence), 0) FROM outcomes"
        ).fetchone()
        if latest != self._cursor:
            self._rebuild()

    def _rebuild(self) -> None:
        """Fold every outcome in order. ``applied`` outcomes re-execute the recorded command
        with the recorded effects fed back; nothing is re-decided. Any other outcome is a
        no-op on the books that still advances the cursor."""
        self._check_chains()
        ledger = Ledger.empty(self._definition.chart)
        registry = self._definition.registry
        rows = self._conn.execute(
            "SELECT o.journal_sequence, o.outcome, op.command, o.entry_id, o.posted_at,"
            " o.head_after FROM outcomes o JOIN operations op ON op.journal_sequence = o.operation"
            " ORDER BY o.journal_sequence"
        ).fetchall()
        cursor = 0
        for seq, outcome, command_json, entry_id, posted_at, head_after in rows:
            if outcome == "applied":
                command = decode_command(json.loads(command_json), registry)
                effects = _Effects(self.clock, self.ids, ledger)
                if entry_id is not None:
                    effects.script(entry_id, datetime.fromisoformat(posted_at))
                try:
                    ledger = ledger.execute(command, clock=effects, ids=effects).ledger
                except LedgerError as exc:
                    raise IntegrityError(f"applied outcome {seq} does not replay: {exc}") from exc
                if ledger.head != head_after:
                    raise IntegrityError(
                        f"outcome {seq}: head {ledger.head} differs from recorded {head_after}"
                    )
            cursor = seq
        self._ledger, self._cursor = ledger, cursor

    def _check_chains(self) -> None:
        """The path property the schema implies, asserted as a self-check on rebuild."""
        rows = self._conn.execute(
            "SELECT operation, journal_sequence, previous_outcome, outcome"
            " FROM outcomes ORDER BY operation, journal_sequence"
        ).fetchall()
        tip: dict[int, tuple[int, str]] = {}
        for operation, seq, previous, outcome in rows:
            if operation not in tip:
                if previous is not None:
                    raise IntegrityError(
                        f"outcome {seq}: first outcome of {operation} is not a root"
                    )
            else:
                prev_seq, prev_kind = tip[operation]
                if previous != prev_seq or prev_kind != "awaiting_approval":
                    raise IntegrityError(f"outcome {seq}: chain of operation {operation} is broken")
            tip[operation] = (seq, outcome)

    # -------------------------------------------------------------- helpers

    @contextmanager
    def _txn(self) -> Iterator[None]:
        self._pending_projection = None
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:  # SQLITE_BUSY past the timeout, or a locked file
            raise JournalError(f"journal unavailable: {exc}") from exc
        try:
            yield
            self._conn.execute("COMMIT")
        except BaseException as exc:
            self._pending_projection = None
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise JournalError(f"journal write failed: {exc}") from exc
            raise
        self._advance_projection()

    def _advance_projection(self) -> None:
        pending = self._pending_projection
        if pending is not None:
            self._ledger, self._cursor = pending
            self._pending_projection = None

    def _alloc(self, kind: str) -> int:
        cur = self._conn.execute("INSERT INTO journal (kind) VALUES (?)", (kind,))
        return int(cur.lastrowid)

    def _current_outcome(self, op_seq: int) -> int | None:
        (value,) = self._conn.execute(
            "SELECT MAX(journal_sequence) FROM outcomes WHERE operation = ?", (op_seq,)
        ).fetchone()
        return None if value is None else int(value)

    def _decision(
        self, inv_seq: int, op_seq: int | None, context: PolicyContext, decision: Any
    ) -> int:
        seq = self._alloc("decisions")
        self._conn.execute(
            "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                seq,
                inv_seq,
                op_seq,
                canonical_text(context.serialized()),
                self.policy.version,
                decision.decision,
                decision.matched_rule,
                decision.reason,
                None,
                None,
                None,
            ),
        )
        return seq

    def _outcome(
        self,
        op_seq: int,
        outcome: str,
        dec_seq: int,
        head_before: str,
        head_after: str,
        *,
        entry_id: str | None = None,
        posted_at: datetime | None = None,
        ledger_sequence: int | None = None,
        error: tuple[str, str] | None = None,
    ) -> int:
        seq = self._alloc("outcomes")
        self._conn.execute(
            "INSERT INTO outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                seq,
                op_seq,
                self._current_outcome(op_seq),  # the tip, read under the write lock
                outcome,
                None if error is None else error[0],
                None if error is None else error[1],
                entry_id,
                None if posted_at is None else posted_at.isoformat(),
                head_before,
                head_after,
                self._ledger.sequence if ledger_sequence is None else ledger_sequence,
                dec_seq,
            ),
        )
        if outcome != "applied":
            self._pending_projection = (self._ledger, seq)  # cursor advances, books do not
        return seq

    def _respond(
        self,
        inv_seq: int,
        disposition: str,
        outcome_seq: int | None,
        response_kind: str,
        response: Response,
    ) -> None:
        seq = self._alloc("invocation_responses")
        self._conn.execute(
            "INSERT INTO invocation_responses VALUES (?,?,?,?,?)",
            (seq, inv_seq, disposition, outcome_seq, response_kind),
        )
        self._event(inv_seq, "outbound", response.as_tool_result())

    def _inbound(self, inv_seq: int, request: Request) -> None:
        self._event(
            inv_seq,
            "inbound",
            {"tool": request.tool, "arguments": request.arguments, "call_id": request.call_id},
        )

    def _event(self, inv_seq: int | None, direction: str, body: dict[str, Any]) -> int:
        seq = self._alloc("events")
        self._conn.execute(
            "INSERT INTO events VALUES (?,?,?,?)",
            (seq, inv_seq, direction, json.dumps(body, sort_keys=True, ensure_ascii=False)),
        )
        return seq

    def _render_replay(self, inv_seq: int, outcome_seq: int) -> Response:
        """A replay answers exactly what the invocation that produced the outcome was told,
        rendered from that invocation's stored outbound event, with ``replayed`` set."""
        row = self._conn.execute(
            "SELECT e.body FROM invocation_responses r"
            " JOIN events e ON e.invocation = r.invocation AND e.direction = 'outbound'"
            " WHERE r.outcome = ? ORDER BY r.journal_sequence LIMIT 1",
            (outcome_seq,),
        ).fetchone()
        if row is None:  # pragma: no cover - every outcome is produced by an invocation
            raise IntegrityError(f"outcome {outcome_seq} has no producing invocation")
        body = json.loads(row[0])
        if body["ok"]:
            return Response(
                inv_seq,
                "replay",
                "replayed",
                True,
                result={**body["result"], "replayed": True},
                outcome=outcome_seq,
            )
        return Response(
            inv_seq,
            "replay",
            "replayed",
            False,
            error_type=body["error"]["type"],
            error_message=body["error"]["message"],
            outcome=outcome_seq,
        )


# ------------------------------------------------------------------- helpers


def _applied_result(applied: Applied) -> dict[str, Any]:
    out: dict[str, Any] = {
        "replayed": applied.replayed,
        "head": applied.ledger.head,
        "sequence": applied.ledger.sequence,
    }
    if applied.entry is not None:
        out["entry_id"] = applied.entry.entry_id
        out["posted_at"] = applied.entry.posted_at.isoformat()
    if applied.transaction is not None:
        out["transaction"] = {
            "id": applied.transaction.transaction_id,
            "status": applied.transaction.status.value,
        }
    return out


def _encode_chart(chart: ChartOfAccounts, admitter: Admitter) -> list[dict[str, Any]]:
    return [
        {
            "account_id": a.account_id,
            "kind": a.kind.value,
            "currency": a.currency.code,
            "allow_negative": a.allow_negative,
            "name": admitter.redact_text(a.name),  # definition free text: class 1
        }
        for a in chart.values()
    ]


def _decode_chart(doc: list[dict[str, Any]], currencies: Mapping[str, Currency]) -> ChartOfAccounts:
    return ChartOfAccounts(
        Account(
            a["account_id"],
            AccountType(a["kind"]),
            currencies[a["currency"]],
            a["allow_negative"],
            a["name"],
        )
        for a in doc
    )


def _is_identifier(value: str) -> bool:
    try:
        require_identifier(value, "call_id")
    except InvalidIdentifierError:
        return False
    return True


def _bounded_utf8(text: str, limit: int = ENVELOPE_BOUND) -> str:
    """Truncate to at most ``limit`` UTF-8 bytes without splitting a character."""
    data = text.encode("utf-8")
    return text if len(data) <= limit else data[:limit].decode("utf-8", errors="ignore")
