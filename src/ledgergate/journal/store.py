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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ledgergate.codec import (
    CODEC_VERSION,
    decode_command,
    digest,
    encode_command,
)
from ledgergate.journal.admission import (
    AdmissionError,
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
    Ledger,
    LedgerError,
    Money,
    command_fingerprint,
)

LOCAL_PRINCIPAL = "local"
ENVELOPE_BOUND = 4096


class JournalError(Exception):
    """A failure the journal cannot record: the transaction is rolled back and nothing is
    written. Stated rather than hidden; see the specification's failure list."""


class IntegrityError(JournalError):
    """The journal's own consistency check failed during rebuild."""


class ConfigurationError(JournalError):
    """The policy set behaved in a way the protocol forbids."""


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


class _Effects:
    """Feeds recorded effects back to the core on rebuild; on live execution, delegates."""

    def __init__(self, clock: Clock, ids: IdGenerator) -> None:
        self._clock, self._ids = clock, ids
        self._scripted: tuple[str, datetime] | None = None

    def script(self, entry_id: str, posted_at: datetime) -> None:
        self._scripted = (entry_id, posted_at)

    def next_id(self) -> str:
        return self._scripted[0] if self._scripted else self._ids.next_id()

    def now(self) -> datetime:
        if self._scripted:
            at = self._scripted[1]
            self._scripted = None
            return at
        return self._clock.now()


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
        definition = Definition(
            journal_id=secrets.token_hex(16),
            chart=chart,
            currencies=dict(currencies or {}),
            policy_set_version=self.policy.version,
        )
        with self._txn():
            seq = self._alloc("definition")
            self._conn.execute(
                "INSERT INTO definition VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    seq,
                    definition.journal_id,
                    SCHEMA_VERSION,
                    definition.codec_version,
                    definition.policy_set_version,
                    definition.token_domain,
                    definition.token_key_version,
                    definition.approval_key,
                    json.dumps(_encode_chart(chart), sort_keys=True),
                    json.dumps(
                        {c: cur.exponent for c, cur in definition.registry.items()}, sort_keys=True
                    ),
                    self.clock.now().astimezone(UTC).isoformat(),
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
        self._conn = connect(path)
        create_schema(self._conn)
        row = self._conn.execute(
            "SELECT journal_id, codec_version, policy_set_version, token_domain,"
            " token_key_version, approval_key, chart, currencies FROM definition"
        ).fetchone()
        if row is None:
            self._conn.close()
            raise JournalError("no definition; use create()")
        currencies = {code: Currency(code, exp) for code, exp in json.loads(row[7]).items()}
        chart = _decode_chart(json.loads(row[6]), currencies)
        self._definition = Definition(
            row[0], chart, currencies, row[1], row[2], row[3], row[4], row[5]
        )
        if self._definition.policy_set_version != self.policy.version:
            self._conn.close()
            raise ConfigurationError(
                f"journal was defined with policy set {row[2]!r};"
                f" this process runs {self.policy.version!r}"
            )
        self._ledger = Ledger.empty(chart)
        self._cursor = 0
        try:
            self._rebuild()
        except JournalError:
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
        """One invocation, one transaction. ``value`` is an already-decoded I-JSON value."""
        with self._txn():
            self._ensure_current()  # step 2
            try:
                request = self.admitter.admit(value, self._definition.registry)  # step 3
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
                    json.dumps({"role": role, "content": content}, sort_keys=True),
                ),
            )
            return seq

    # ---------------------------------------------------------------- write

    def _write(self, request: Request) -> Response:
        assert request.command is not None and request.key is not None
        command = request.command
        fingerprint = command_fingerprint(command)
        now = self.clock.now().astimezone(UTC)
        row = self._conn.execute(
            "SELECT journal_sequence, fingerprint FROM operations WHERE key = ?", (request.key,)
        ).fetchone()

        # step 4: resolve the key and write the invocation
        if row is None:
            op_seq = self._alloc("operations")
            self._conn.execute(
                "INSERT INTO operations VALUES (?,?,?,?)",
                (
                    op_seq,
                    request.key,
                    fingerprint,
                    json.dumps(encode_command(command), sort_keys=True),
                ),
            )
            disposition = "new"
        elif row[1] == fingerprint:
            op_seq = row[0]
            current = self._current_outcome(op_seq)
            disposition = "replay"  # approvals are refused at admission in M2b
            if current is None:  # pragma: no cover - invariant 2 forbids this
                raise IntegrityError("operation without an outcome")
        else:
            op_seq = row[0]
            disposition = "conflict"

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
                json.dumps(encode_command(command), sort_keys=True),
                request.request_digest(),
                request.call_id,
            ),
        )
        self._event(
            inv_seq,
            "inbound",
            {"tool": request.tool, "arguments": request.arguments, "call_id": request.call_id},
        )  # step 5

        # step 6: short paths
        if disposition == "replay":
            outcome_seq = self._current_outcome(op_seq)
            assert outcome_seq is not None
            response = self._render_outcome(inv_seq, outcome_seq, "replayed", disposition)
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
        dec_seq = self._alloc("decisions")
        self._conn.execute(
            "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                dec_seq,
                inv_seq,
                op_seq,
                json.dumps(context.serialized(), sort_keys=True),
                self.policy.version,
                decision.decision,
                decision.matched_rule,
                decision.reason,
                None,
                None,
                None,
            ),
        )
        head = self._ledger.head
        if decision.decision == "deny":
            outcome_seq = self._outcome(op_seq, None, "denied", dec_seq, head, head, None, None)
            response = Response(
                inv_seq,
                disposition,
                "denied",
                False,
                error_type="PolicyDenied",
                error_message=f"{decision.matched_rule}: {decision.reason}",
                outcome=outcome_seq,
            )
            self._respond(inv_seq, disposition, outcome_seq, "denied", response)
            return response
        if decision.decision == "approval_required":
            outcome_seq = self._outcome(
                op_seq, None, "awaiting_approval", dec_seq, head, head, None, None
            )
            response = Response(
                inv_seq,
                disposition,
                "awaiting_approval",
                False,
                error_type="ApprovalRequired",
                error_message=f"{decision.matched_rule}: {decision.reason}",
                outcome=outcome_seq,
            )
            self._respond(inv_seq, disposition, outcome_seq, "awaiting_approval", response)
            return response

        # step 8: execute through the pure core
        effects = _Effects(self.clock, self.ids)
        try:
            applied = self._ledger.execute(command, clock=effects, ids=effects)
        except LedgerError as exc:
            outcome_seq = self._outcome(
                op_seq,
                None,
                "rejected",
                dec_seq,
                head,
                head,
                None,
                None,
                error=(type(exc).__name__, str(exc)),
            )
            response = Response(
                inv_seq,
                disposition,
                "rejected",
                False,
                error_type=type(exc).__name__,
                error_message=str(exc),
                outcome=outcome_seq,
            )
            self._respond(inv_seq, disposition, outcome_seq, "rejected", response)
            return response

        # step 9: append the applied outcome
        entry = applied.entry
        outcome_seq = self._outcome(
            op_seq,
            None,
            "applied",
            dec_seq,
            head,
            applied.ledger.head,
            None if entry is None else entry.entry_id,
            None if entry is None else entry.posted_at,
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
        # The projection advances only when the transaction commits; _txn handles rollback.
        self._pending_projection = (applied.ledger, outcome_seq)
        return response

    # ----------------------------------------------------------------- read

    def _read(self, request: Request) -> Response:
        now = self.clock.now().astimezone(UTC)
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
        self._event(
            inv_seq,
            "inbound",
            {"tool": request.tool, "arguments": request.arguments, "call_id": request.call_id},
        )
        if self.policy.gates_read(request.tool):
            context = PolicyContext(
                self.principal, None, request.request_digest(), "request", now, self.policy.version
            )
            decision = self.policy.evaluate(context)
            dec_seq = self._alloc("decisions")
            self._conn.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    dec_seq,
                    inv_seq,
                    None,
                    json.dumps(context.serialized(), sort_keys=True),
                    self.policy.version,
                    decision.decision,
                    decision.matched_rule,
                    decision.reason,
                    None,
                    None,
                    None,
                ),
            )
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
        try:
            result = self._serve(request)
        except (LedgerError, AdmissionError) as exc:
            response = Response(
                inv_seq,
                "read",
                "read",
                False,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            self._respond(inv_seq, "read", None, "read", response)
            return response
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
            account = request.arguments.get("account")
            if not isinstance(account, str):
                raise AdmissionError("missing_field", "arguments.account")
            money = self._ledger.balance(account)
            return {"account": account, "balance": _money_str(money), "cursor": self._cursor}
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
        now = self.clock.now().astimezone(UTC)
        call_id = value.get("call_id") if isinstance(value, dict) else None
        tool = value.get("tool") if isinstance(value, dict) else None
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
                call_id if isinstance(call_id, str) and _is_identifier(call_id) else None,
            ),
        )
        blob = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)[:ENVELOPE_BOUND]
        envelope = {
            "call_id": call_id if isinstance(call_id, str) and _is_identifier(call_id) else None,
            "tool": tool if isinstance(tool, str) and tool in _known_tools() else None,
            "input_digest": _input_digest(value),
            "error": {"code": exc.code, "path": exc.path or "$"},
            "payload": blob,  # identity admitter: pass-through; M2c redacts
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
        latest = self._conn.execute(
            "SELECT COALESCE(MAX(journal_sequence), 0) FROM outcomes"
        ).fetchone()[0]
        if latest != self._cursor:
            self._rebuild()

    def _rebuild(self) -> None:
        ledger = Ledger.empty(self._definition.chart)
        registry = self._definition.registry
        rows = self._conn.execute(
            "SELECT o.journal_sequence, o.outcome, op.command, o.entry_id, o.posted_at,"
            " o.head_after"
            " FROM outcomes o JOIN operations op ON op.journal_sequence = o.operation"
            " ORDER BY o.journal_sequence"
        ).fetchall()
        cursor = 0
        for seq, outcome, command_json, entry_id, posted_at, head_after in rows:
            if outcome == "applied":
                command = decode_command(json.loads(command_json), registry)
                effects = _Effects(self.clock, self.ids)
                if entry_id is not None:
                    effects.script(entry_id, datetime.fromisoformat(posted_at))
                try:
                    ledger = ledger.execute(command, clock=effects, ids=effects).ledger
                except LedgerError as exc:
                    raise IntegrityError(f"applied outcome {seq} does not replay: {exc}") from exc
                if ledger.head != head_after:
                    raise IntegrityError(
                        f"outcome {seq}: head {ledger.head} != recorded {head_after}"
                    )
            cursor = seq
        self._ledger, self._cursor = ledger, cursor

    # -------------------------------------------------------------- helpers

    @contextmanager
    def _txn(self) -> Iterator[None]:
        self._pending_projection = None
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            self._pending_projection = None
            raise
        self._conn.execute("COMMIT")
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
        row = self._conn.execute(
            "SELECT MAX(journal_sequence) FROM outcomes WHERE operation = ?", (op_seq,)
        ).fetchone()
        value = row[0]
        return None if value is None else int(value)

    def _outcome(
        self,
        op_seq: int,
        previous: int | None,
        outcome: str,
        dec_seq: int,
        head_before: str,
        head_after: str,
        entry_id: str | None,
        posted_at: datetime | None,
        *,
        ledger_sequence: int | None = None,
        error: tuple[str, str] | None = None,
    ) -> int:
        seq = self._alloc("outcomes")
        self._conn.execute(
            "INSERT INTO outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                seq,
                op_seq,
                previous,
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
        # A non-applied outcome still advances the cursor on commit.
        if outcome != "applied":
            self._pending_projection = (self._ledger, seq)
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

    def _event(self, inv_seq: int | None, direction: str, body: dict[str, Any]) -> int:
        seq = self._alloc("events")
        self._conn.execute(
            "INSERT INTO events VALUES (?,?,?,?)",
            (seq, inv_seq, direction, json.dumps(body, sort_keys=True, ensure_ascii=False)),
        )
        return seq

    def _render_outcome(
        self, inv_seq: int, outcome_seq: int, response_kind: str, disposition: str
    ) -> Response:
        row = self._conn.execute(
            "SELECT o.outcome, o.error_type, o.error_message, o.entry_id, o.head_after,"
            " o.ledger_sequence, d.matched_rule, d.reason"
            " FROM outcomes o LEFT JOIN decisions d ON d.journal_sequence = o.decision"
            " WHERE o.journal_sequence = ?",
            (outcome_seq,),
        ).fetchone()
        outcome, error_type, error_message, entry_id, head_after, ledger_sequence, rule, reason = (
            row
        )
        if outcome in ("denied", "awaiting_approval"):
            error_message = f"{rule}: {reason}"
        if outcome == "applied":
            return Response(
                inv_seq,
                disposition,
                response_kind,
                True,
                result={
                    "replayed": True,
                    "entry_id": entry_id,
                    "head": head_after,
                    "sequence": ledger_sequence,
                },
                outcome=outcome_seq,
            )
        etype = {"denied": "PolicyDenied", "awaiting_approval": "ApprovalRequired"}.get(
            outcome, error_type
        )
        return Response(
            inv_seq,
            disposition,
            response_kind,
            False,
            error_type=etype,
            error_message=error_message or outcome,
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


def _encode_chart(chart: ChartOfAccounts) -> list[dict[str, Any]]:
    return [
        {
            "account_id": a.account_id,
            "kind": a.kind.value,
            "currency": a.currency.code,
            "allow_negative": a.allow_negative,
            "name": a.name,
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
    from ledgergate.ledger import InvalidIdentifierError
    from ledgergate.ledger.identifiers import require_identifier

    try:
        require_identifier(value, "call_id")
    except InvalidIdentifierError:
        return False
    return True


def _known_tools() -> frozenset[str]:
    from ledgergate.journal.admission import TOOLS

    return TOOLS


def _input_digest(value: Any) -> str:
    try:
        return digest(value)
    except Exception:  # not I-JSON; the transport should have refused it
        return digest(json.dumps(value, sort_keys=True, default=str))
