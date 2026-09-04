# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""The journal: a strictly append-only SQLite record of every attempt to move money.

This module implements the write and read protocols of ``docs/spec/journal.md`` step for
step. The in-memory :class:`~ledgergate.ledger.Ledger` is a projection rebuilt from
``outcomes``; the journal is the only durable truth. One invocation is one
``BEGIN IMMEDIATE`` transaction, the response is rendered only after commit, and every
row is written after every row it references; the foreign keys fix that partial order and
the protocol's step order is one linearization of it.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import sqlite3
import warnings
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from ledgergate.codec import (
    CODEC_VERSION,
    MAX_PAYLOAD_NODES,
    MAX_TEXT,
    CodecError,
    IJsonError,
    canonical_text,
    decode_command,
    digest,
    encode_command,
    looks_sensitive,
    payload_size,
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
from ledgergate.journal.approvals import (
    Approval,
    CheckResult,
    Verdict,
    check,
    signature_verifies,
    verification_key,
)
from ledgergate.journal.policy import (
    Decision,
    NullPolicySet,
    PolicyContext,
    PolicySet,
    command_amount,
    command_kind,
)
from ledgergate.journal.schema import (
    JOURNAL_TABLES,
    SCHEMA_VERSION,
    connect,
    create_schema,
    probe,
    tables_of,
)
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

T = TypeVar("T")
LOCAL_PRINCIPAL = "local"
ENVELOPE_BOUND = 4096  # bytes of UTF-8, per the specification
MAX_MESSAGE_CHARS = 65536  # the trace schema's bound on message content
_BOUND = frozenset({"path", "clock", "ids", "admitter", "policy", "principal"})


AGGREGATE_NAME = re.compile(r"applied\.[a-z_]+\.[A-Z]{3}\.[1-9][0-9]*s")
DECIMAL_TEXT = re.compile(r"-?[0-9]{1,40}")


def _bounded_subject(subject: Any) -> str | None:
    """A set's subject is an identifier or nothing; anything else is a configuration fault,
    so the context a trace carries is always one the trace models accept."""
    if subject is None:
        return None
    try:
        return require_identifier(subject, "policy subject")
    except (InvalidIdentifierError, TypeError) as exc:
        raise ConfigurationError(f"policy set returned an unusable subject: {exc}") from exc


def _bounded_aggregates(aggregates: Any) -> dict[str, str]:
    """Aggregates are ``applied.<kind>.<CCY>.<W>s -> decimal string``, the grammar the trace
    enforces; a set returning anything else is misconfigured."""
    if not isinstance(aggregates, Mapping) or any(
        not isinstance(k, str)
        or not isinstance(v, str)
        or not AGGREGATE_NAME.fullmatch(k)
        or not DECIMAL_TEXT.fullmatch(v)
        for k, v in aggregates.items()
    ):
        raise ConfigurationError("policy set returned aggregates outside the recorded grammar")
    return dict(aggregates)


def _configuration_text(policy: PolicySet) -> str | None:
    doc = policy.configuration()
    return None if doc is None else canonical_text(doc)


MESSAGE_ROLES = frozenset({"system", "user", "assistant", "tool"})


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

    def __post_init__(self) -> None:
        # Error messages are bounded by construction (fixed text plus identifiers of at most
        # 256 characters); a longer one is a bug in whoever built it, and would leave the
        # journal with a row the trace cannot carry, so it is refused before any write.
        if self.error_message is not None and len(self.error_message) > MAX_TEXT:
            raise IntegrityError("error message exceeds the trace bound")

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
    token_check: str = "none"  # noqa: S105 - identifies the key; not key material
    policy_config: str = "none"
    approval_key: str = "none"
    policy_configuration: str | None = None
    """The set's declarative rules as JCS text, when it has any; a trace carries it so a
    verifier can recompute decisions. ``policy_config`` is its digest."""

    @property
    def registry(self) -> dict[str, Currency]:
        """Exactly the currencies the definition recorded. The process's bundled table is
        folded in once, at creation; a later build's additions never leak into an old
        journal, so two processes on one journal accept the same currency set."""
        return dict(self.currencies)

    @staticmethod
    def full_registry(
        chart: ChartOfAccounts, extra: Mapping[str, Currency] | None
    ) -> dict[str, Currency]:
        out = dict(CURRENCIES)
        out.update(extra or {})
        out.update(chart.currencies())
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
        if self._ledger.has_entry(entry_id):
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
    _approval_key: str = field(init=False, default="none", repr=False)

    def __setattr__(self, name: str, value: Any) -> None:
        # The components a definition binds (policy, admitter, principal, effects) are
        # fixed for the life of the object: swapping one after open would let calls run
        # under rules or a redaction key the definition never recorded.
        if name in _BOUND and name in self.__dict__:
            raise ConfigurationError(f"Journal.{name} is bound at open and cannot be replaced")
        super().__setattr__(name, value)

    def _check_binding(self) -> None:
        """Re-assert, at the start of every transaction, that the components in use are the
        ones the definition recorded; ``open`` checked once, and this makes the check hold
        for every call rather than for the first."""
        d = self._definition
        if (
            self.policy.version != d.policy_set_version
            or self.policy.configuration_digest() != d.policy_config
            or (self.admitter.token_domain, self.admitter.token_key_version)
            != (d.token_domain, d.token_key_version)
            or not hmac.compare_digest(self.admitter.key_check(), d.token_check)
        ):
            raise ConfigurationError(
                "the policy set or admitter in use no longer matches the journal's definition"
            )

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
        approval_key: str = "none",
    ) -> Journal:
        """``approval_key`` is the base64url Ed25519 verification key approvals must verify
        against; ``none`` means no artefact can ever verify."""
        if approval_key != "none":
            verification_key(approval_key)  # refuse a malformed key at creation
        elif getattr(policy, "approve_above", ()):
            raise ConfigurationError(
                "the policy set can require approval but the journal has no verification key,"
                " so nothing could ever approve; supply approval_key or drop approve_above"
            )
        self = cls(path, clock, ids, admitter or IdentityAdmitter(), policy or NullPolicySet())
        self._approval_key = approval_key
        target = Path(path)
        if target.exists() and target.stat().st_size > 0:
            # Inspect read-only before any pragma touches the file. Only an empty database
            # or a complete journal may proceed; anything else, including a database whose
            # table names merely overlap the journal's, is refused byte-for-byte unchanged.
            try:
                tables = {n for n in tables_of(path) if not n.startswith("sqlite_")}
            except sqlite3.Error as exc:
                raise JournalError(f"cannot create journal at {path}: {exc}") from exc
            if tables and tables != JOURNAL_TABLES:
                raise JournalError(f"{path} is a database but not a journal; refusing to add to it")
            if tables:
                try:
                    defined = _read_definition_row(path) is not None
                except sqlite3.Error as exc:
                    raise JournalError(f"cannot create journal at {path}: {exc}") from exc
                if defined:
                    raise JournalError("journal already has a definition; use open()")
        try:
            self._conn = connect(path)
        except sqlite3.Error as exc:
            raise JournalError(f"cannot create journal at {path}: {exc}") from exc
        try:
            create_schema(self._conn)
            if self._conn.execute("SELECT 1 FROM definition").fetchone():
                raise JournalError("journal already has a definition; use open()")
            return cls._define(self, chart, currencies)
        except sqlite3.Error as exc:
            self._conn.close()
            raise JournalError(f"cannot create journal at {path}: {exc}") from exc
        except JournalError:
            self._conn.close()
            raise

    @classmethod
    def _define(
        cls, self: Journal, chart: ChartOfAccounts, currencies: Mapping[str, Currency] | None
    ) -> Journal:
        for account in chart.values():
            if looks_sensitive(account.account_id):
                warnings.warn(
                    f"account id {account.account_id!r} looks like an email, phone or card"
                    " number; operator-defined identifiers are stored as given",
                    stacklevel=3,
                )
        # Every result the journal will ever serve must be representable in a trace: the
        # trial balance grows with the chart, so the chart is bounded by the payload bound.
        zero = {"amount": "0", "currency": "XXX"}
        probe_rows = [
            {"account": a.account_id, "debit": zero, "credit": zero} for a in chart.values()
        ]  # the exact shape _serve renders
        nodes, _depth = payload_size({"rows": probe_rows, "balanced": True, "cursor": 0})
        if nodes > MAX_PAYLOAD_NODES:
            raise ConfigurationError(
                f"a chart of {len(chart)} accounts serves a trial balance of {nodes} nodes,"
                f" above the trace payload bound of {MAX_PAYLOAD_NODES}"
            )
        definition = Definition(
            journal_id=secrets.token_hex(16),
            chart=chart,
            currencies=Definition.full_registry(chart, currencies),
            policy_set_version=self.policy.version,
            token_domain=self.admitter.token_domain,
            token_key_version=self.admitter.token_key_version,
            token_check=self.admitter.key_check(),
            policy_config=self.policy.configuration_digest(),
            approval_key=self._approval_key,
            policy_configuration=_configuration_text(self.policy),
        )
        registry = definition.registry
        with self._txn():
            seq = self._alloc("definition")
            self._conn.execute(
                "INSERT INTO definition VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    seq,
                    1,
                    definition.journal_id,
                    SCHEMA_VERSION,
                    definition.codec_version,
                    definition.policy_set_version,
                    definition.token_domain,
                    definition.token_key_version,
                    definition.token_check,
                    definition.policy_config,
                    definition.policy_configuration,
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
            probe(path)  # read-only: a foreign file is refused before any pragma touches it
            row = _read_definition_row(path)  # also read-only; refuses another schema version
        except (sqlite3.Error, ValueError) as exc:
            raise JournalError(f"cannot open journal at {path}: {exc}") from exc
        if row is None:
            raise JournalError(f"cannot open journal at {path}: no definition; use create()")
        if row[1] != CODEC_VERSION:
            raise ConfigurationError(
                f"journal is codec {row[1]!r}; this process is codec {CODEC_VERSION!r}"
            )
        if row[2] != self.policy.version:
            raise ConfigurationError(
                f"journal was defined with policy set {row[2]!r};"
                f" this process runs {self.policy.version!r}"
            )
        if row[10] != self.policy.configuration_digest():
            raise ConfigurationError(
                f"policy set {self.policy.version!r} has different rules from the ones this"
                " journal was defined with; a rule change is a new journal"
            )
        if (row[3], row[4]) != (self.admitter.token_domain, self.admitter.token_key_version):
            raise ConfigurationError(
                f"journal tokens are {row[3]!r}/{row[4]!r}; this admitter is"
                f" {self.admitter.token_domain!r}/{self.admitter.token_key_version!r}"
            )
        if not hmac.compare_digest(row[9], self.admitter.key_check()):
            raise ConfigurationError(
                "this admitter's key does not reproduce the journal's token check;"
                " a different key under the same label would fork the identifier space"
            )
        try:
            self._conn = connect(path, create=False)
        except sqlite3.Error as exc:
            raise JournalError(f"cannot open journal at {path}: {exc}") from exc
        try:
            try:
                currencies = {code: Currency(code, exp) for code, exp in json.loads(row[7]).items()}
                chart = _decode_chart(json.loads(row[6]), currencies)
            except (ValueError, KeyError, TypeError, LedgerError) as exc:
                raise IntegrityError(f"definition does not decode: {exc}") from exc
            self._definition = Definition(
                row[0],
                chart,
                currencies,
                row[1],
                row[2],
                row[3],
                row[4],
                row[9],
                row[10],
                row[5],
                row[11],
            )
            self._ledger = Ledger.empty(chart)
            self._cursor = 0
            self._conn.execute("BEGIN")  # one snapshot for the chain check and the fold
            try:
                self._rebuild()
            finally:
                self._conn.execute("COMMIT")
        except JournalError:
            self._conn.close()
            raise
        except sqlite3.Error as exc:
            self._conn.close()
            raise JournalError(f"cannot open journal at {path}: {exc}") from exc
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
        with self._txn():
            self._check_binding()
            self._ensure_current()  # step 2
            scope = AdmissionScope(
                self._definition.registry, self._definition.chart, self.principal, self._ledger
            )
            try:
                request = self.admitter.admit(value, scope)  # step 3
            except AdmissionError as exc:
                return self._invalid(value, exc)
            if request.is_read:
                return self._read(request)
            return self._write(request)

    def record_message(self, role: str, content: str) -> int:
        """A standalone message event: its own transaction, no invocation. ``role`` is one
        of the trace schema's four; only ``content`` is free text."""
        if role not in MESSAGE_ROLES:
            raise ValueError(f"role must be one of {sorted(MESSAGE_ROLES)}")
        if len(content) > MAX_MESSAGE_CHARS:
            raise ValueError(f"message content exceeds {MAX_MESSAGE_CHARS} characters")
        with self._txn():
            self._check_binding()
            seq = self._alloc("events")
            self._conn.execute(
                "INSERT INTO events VALUES (?,?,?,?)",
                (
                    seq,
                    None,
                    "message",
                    json.dumps(
                        {
                            "role": role,
                            "content": self.admitter.redact_text(content),
                            "at": _Effects.aware_now(self.clock).isoformat(),
                        },
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
        presented = request.approval is not None
        if row is None:
            op_seq = self._alloc("operations")
            self._conn.execute(
                "INSERT INTO operations VALUES (?,?,?,?)",
                (op_seq, request.key, fingerprint, encoded),
            )
            disposition = "new"
        elif row[1] == fingerprint:
            op_seq = row[0]
            current = self._current_outcome_kind(op_seq)
            disposition = "approval" if current == "awaiting_approval" and presented else "replay"
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

        # An artefact presented where none was expected is kept, not dropped.
        presentation: int | None = None
        verdict: Verdict | None = None
        consumption: int | None = None
        if presented and disposition != "approval":
            presentation = self._present(inv_seq, request, "approval_not_applicable")
            verdict = "approval_not_applicable"

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
        if disposition == "approval":
            presentation, verdict, consumption = self._validate_approval(
                inv_seq, request, now, fingerprint
            )

        # step 7: decide
        approval_ctx = (
            None if verdict is None else {"presentation": presentation, "verdict": verdict}
        )
        money = command_amount(command)
        failed_verdict = verdict is not None and verdict not in (
            "approval_valid",
            "approval_not_applicable",
        )
        # On a failed verdict no policy code runs at all, not even subject derivation or
        # aggregate reads: the runtime decides from the verdict alone, and the persisted
        # context says so with a null subject and no aggregates.
        context = PolicyContext(
            principal=self.principal,
            subject=None
            if failed_verdict
            else _bounded_subject(self._guarded(lambda: self.policy.subject_of(command))),
            command_digest=fingerprint,
            digest_kind="fingerprint",
            evaluated_at=now,
            policy_set_version=self.policy.version,
            command_kind=command_kind(command),
            amount=None if money is None else str(money.amount),
            currency=None if money is None else money.currency.code,
            aggregates={}
            if failed_verdict
            else _bounded_aggregates(
                self._guarded(lambda: self.policy.aggregates_for(command, now, _History(self)))
            ),
            approval=approval_ctx,
        )
        if failed_verdict:
            # A failed verdict: the runtime decides; the policy set never sees it.
            assert verdict is not None
            decision = Decision("deny", "runtime.approval_rejected", verdict)
        else:
            decision = self._guarded(lambda: self.policy.evaluate(context))
            self._refuse_runtime_namespace(decision)
            if decision.decision == "approval_required" and verdict == "approval_valid":
                raise ConfigurationError(
                    "policy set asked for approval after a valid approval was consumed;"
                    " the rule that required it has been satisfied and the set is misconfigured"
                )
        dec_seq = self._decision(
            inv_seq, op_seq, context, decision, presentation, verdict, consumption
        )
        head = self._ledger.head
        if failed_verdict:
            # Pending-operation table: a failed verdict appends no outcome. The operation
            # stays at its awaiting_approval tip, this response names that tip, and a plain
            # retry therefore replays what the operation's own request was told
            # (ApprovalRequired), never a verdict on an artefact it did not present.
            tip = self._current_outcome(op_seq)
            if tip is None:  # pragma: no cover - the approval disposition requires a tip
                raise IntegrityError(f"operation {op_seq} has no outcome")
            response = Response(
                inv_seq,
                disposition,
                "awaiting_approval",
                False,
                error_type="ApprovalRejected",
                error_message=f"{decision.matched_rule}: {decision.reason}",
                outcome=tip,
            )
            self._respond(inv_seq, disposition, tip, "awaiting_approval", response)
            return response
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
            # The core saw the admitted command, so its message carries only tokens and
            # operator identifiers; it is recorded as is, and a derived trace replays it.
            message = str(exc)
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
        presentation: int | None = None
        if request.approval is not None:
            presentation = self._present(inv_seq, request, "approval_not_applicable")
        if self.policy.gates_read(request.tool):
            verdict: Verdict | None = None if presentation is None else "approval_not_applicable"
            context = PolicyContext(
                self.principal,
                None,
                request.request_digest(),
                "request",
                now,
                self.policy.version,
                approval=None
                if verdict is None
                else {"presentation": presentation, "verdict": verdict},
            )
            decision = self._guarded(lambda: self.policy.evaluate(context))
            self._refuse_runtime_namespace(decision)
            if decision.decision == "approval_required":
                raise ConfigurationError(
                    "a read cannot await approval: the policy set returned approval_required"
                    " for a read intent, which has no operation to approve"
                )
            self._decision(inv_seq, None, context, decision, presentation, verdict)
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
            "input_digest": self.admitter.digest_input(value),
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
                effects = _Effects(self.clock, self.ids, ledger)
                try:
                    command = decode_command(json.loads(command_json), registry)
                    if entry_id is not None:
                        effects.script(entry_id, datetime.fromisoformat(posted_at))
                except (CodecError, LedgerError, ValueError) as exc:
                    raise IntegrityError(f"applied outcome {seq} does not decode: {exc}") from exc
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
        self,
        inv_seq: int,
        op_seq: int | None,
        context: PolicyContext,
        decision: Decision,
        presentation: int | None = None,
        verdict: Verdict | None = None,
        consumption: int | None = None,
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
                presentation,
                verdict,
                consumption,
            ),
        )
        return seq

    def _refuse_runtime_namespace(self, decision: Decision) -> None:
        """``runtime.`` rules are written by the runtime alone; a set that names one is
        misconfigured, and the fault is unrecorded like every other configuration fault."""
        if decision.matched_rule.startswith("runtime."):
            raise ConfigurationError(
                f"policy set {self.policy.version!r} returned rule {decision.matched_rule!r};"
                " the runtime. namespace is reserved for the runtime's own decisions"
            )

    def _guarded(self, call: Callable[[], T]) -> T:
        """A policy set is a pure function of its context; raising is a bug in the set, an
        unrecorded failure, and named as such on every path that invokes it. A decision's
        rule and reason are bounded like every other short text a trace carries."""
        try:
            result = call()
        except Exception as exc:
            raise ConfigurationError(f"policy set {self.policy.version!r} raised: {exc}") from exc
        if isinstance(result, Decision) and (
            len(result.matched_rule) > MAX_TEXT or len(result.reason) > MAX_TEXT
        ):
            raise ConfigurationError(
                f"policy set {self.policy.version!r} returned a rule or reason over {MAX_TEXT}"
                " characters"
            )
        return result

    def _current_outcome_kind(self, op_seq: int) -> str | None:
        row = self._conn.execute(
            "SELECT outcome FROM outcomes WHERE operation = ?"
            " ORDER BY journal_sequence DESC LIMIT 1",
            (op_seq,),
        ).fetchone()
        return None if row is None else str(row[0])

    def _present(
        self, inv_seq: int, request: Request, result: CheckResult, verified: bool | None = None
    ) -> int:
        """The approvals presentation row: one per presentation, carrying the pure-check
        result; the verdict lives on the decision. Identity and display fields are stored
        only once the signature verified: until then they are the presenter's words, and a
        presentation row holds nothing but fixed-grammar bindings and the signature."""
        assert request.approval is not None
        a = Approval.from_json(request.approval)
        if verified is None:
            verified = self._signature_verifies(a)
        seq = self._alloc("approvals")
        self._conn.execute(
            "INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                seq,
                inv_seq,
                a.journal_id,
                a.approval_id if verified else None,
                a.approver if verified else None,
                a.fingerprint,
                a.key if verified else None,
                a.subject if verified else None,
                a.amount if verified else None,
                a.currency if verified else None,
                a.issued_at.isoformat(),
                a.expires_at.isoformat(),
                a.signature,
                int(verified),
                result,
            ),
        )
        return seq

    def _signature_verifies(self, artefact: Approval) -> bool:
        if self._definition.approval_key == "none":
            return False
        return signature_verifies(artefact, verification_key(self._definition.approval_key))

    def _validate_approval(
        self, inv_seq: int, request: Request, now: datetime, fingerprint: str
    ) -> tuple[int, Verdict, int | None]:
        """Checks 1 to 3, then the presentation row, then consumption (check 4)."""
        assert request.approval is not None and request.key is not None
        artefact = Approval.from_json(request.approval)
        if self._definition.approval_key == "none":
            result: CheckResult = "approval_invalid"  # no verification key: nothing verifies
        else:
            result = check(
                artefact,
                public=verification_key(self._definition.approval_key),
                now=now,
                journal_id=self._definition.journal_id,
                fingerprint=fingerprint,
                key=request.key,
            )
        presentation = self._present(inv_seq, request, result, result != "approval_invalid")
        if result != "checks_passed":
            return presentation, result, None
        used = self._conn.execute(
            "SELECT 1 FROM approval_consumptions WHERE approval_id = ?", (artefact.approval_id,)
        ).fetchone()
        if used is not None:
            return presentation, "approval_already_used", None
        seq = self._alloc("approval_consumptions")
        self._conn.execute(
            "INSERT INTO approval_consumptions VALUES (?,?,?,?)",
            (seq, artefact.approval_id, presentation, inv_seq),
        )
        return presentation, "approval_valid", seq

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


class _History:
    """Reads, inside the admitting transaction, what the policy set asks about the past."""

    def __init__(self, journal: Journal) -> None:
        self._journal = journal

    def applied_total(self, *, subject: str, kind: str, currency: str, since: datetime) -> int:
        j = self._journal
        rows = j._conn.execute(
            "SELECT op.command, i.requested_at FROM outcomes o"
            " JOIN operations op ON op.journal_sequence = o.operation"
            " JOIN invocation_responses r ON r.outcome = o.journal_sequence"
            " JOIN invocations i ON i.journal_sequence = r.invocation"
            " WHERE o.outcome = 'applied' AND r.disposition IN ('new', 'approval')"
        ).fetchall()
        total = 0
        registry = j._definition.registry
        for command_json, requested_at in rows:
            if datetime.fromisoformat(requested_at) < since:
                continue
            command = decode_command(json.loads(command_json), registry)
            money = command_amount(command)
            if (
                command_kind(command) == kind
                and money is not None
                and money.currency.code == currency
                and j.policy.subject_of(command) == subject
            ):
                total += money.amount
        return total


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


def _bounded_name(name: str) -> str:
    if len(name) > MAX_TEXT:
        raise ConfigurationError(f"account name exceeds {MAX_TEXT} characters after redaction")
    return name


def _encode_chart(chart: ChartOfAccounts, admitter: Admitter) -> list[dict[str, Any]]:
    return [
        {
            "account_id": a.account_id,
            "kind": a.kind.value,
            "currency": a.currency.code,
            "allow_negative": a.allow_negative,
            "name": _bounded_name(admitter.redact_text(a.name)),  # definition free text: class 1
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


def _read_definition_row(path: str) -> tuple[Any, ...] | None:
    """The definition row over a read-only connection, so version checks happen before any
    pragma is applied to the file. ``schema_version`` is read first and alone, so a journal
    from another schema version is refused by the version comparison, not by whichever
    column the newer layout happens to lack."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        version = conn.execute("SELECT schema_version FROM definition").fetchone()
        if version is None:
            return None
        if version[0] != SCHEMA_VERSION:
            raise ConfigurationError(
                f"journal is schema {version[0]}; this process is schema {SCHEMA_VERSION}"
            )
        row = conn.execute(
            "SELECT journal_id, codec_version, policy_set_version, token_domain,"
            " token_key_version, approval_key, chart, currencies, schema_version, token_check,"
            " policy_config, policy_configuration FROM definition"
        ).fetchone()
        return None if row is None else tuple(row)
    finally:
        conn.close()


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
