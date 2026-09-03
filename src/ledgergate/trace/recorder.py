# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Record a ledger session as a trace.

The recorder wraps a :class:`~ledgergate.ledger.Ledger` and writes a ``ledger_command``
event before each command and a ``ledger_result`` event after, whether the command
succeeded, replayed or raised. Failures are recorded, not swallowed: the exception
propagates after the result event is written, so a trace of a run that blew up still
shows exactly which command did it.

Timestamps come from the injected clock, so a recorded session is as replayable as the
ledger it records.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import JsonValue

from ledgergate.codec import Tokenizer
from ledgergate.ledger import (
    Applied,
    ChartOfAccounts,
    Clock,
    Command,
    ConflictingCurrencyError,
    Currency,
    IdGenerator,
    Ledger,
    LedgerError,
)
from ledgergate.trace.models import (
    SCHEMA_VERSION,
    AccountDoc,
    AgentDoc,
    CurrencyDoc,
    ErrorDoc,
    LedgerCommandEvent,
    LedgerResultEvent,
    MessageEvent,
    ToolCallEvent,
    ToolResultEvent,
    Trace,
    command_currencies,
    command_doc,
)

EventDoc = MessageEvent | ToolCallEvent | ToolResultEvent | LedgerCommandEvent | LedgerResultEvent


@dataclass
class Recorder:
    """Accumulates events and the ledger they act on. Not thread-safe; one per run."""

    trace_id: str
    agent: AgentDoc
    chart: ChartOfAccounts
    clock: Clock
    ids: IdGenerator
    scenario_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    redactor: Tokenizer | None = None
    ledger: Ledger = field(init=False)
    events: list[EventDoc] = field(default_factory=list)
    _seq: int = field(default=0, init=False)
    _commands: int = field(default=0, init=False)
    _started_at: datetime = field(init=False)
    _currencies: dict[str, Currency] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.ledger = Ledger.empty(self.chart)
        self._started_at = self.clock.now()
        # ChartOfAccounts guarantees one exponent per code, so this cannot conflict.
        self._currencies.update(self.chart.currencies())

    def _register_all(self, currencies: set[Currency]) -> None:
        """Record every currency a command carries, or none of them.

        Validation runs against a candidate copy and commits only if the whole set is
        consistent, both internally (a command carrying CAD/2 and CAD/3 at once) and
        against what is already registered. A rejected command must leave the recorder
        exactly as it found it; otherwise a later valid command could be refused because
        of one that was never recorded. Silently keeping the first exponent seen would
        let a command in CAD/3 be replayed as CAD/2: a tenfold change in meaning that a
        clean replay would then certify.
        """
        candidate = dict(self._currencies)
        for cur in sorted(currencies, key=lambda c: (c.code, c.exponent)):
            known = candidate.setdefault(cur.code, cur)
            if known.exponent != cur.exponent:
                raise ConflictingCurrencyError(cur.code, (known.exponent, cur.exponent))
        self._currencies = candidate

    # ----------------------------------------------------------- primitives

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def message(self, role: str, content: str) -> None:
        if self.redactor is not None:
            content = self.redactor.redact(content)
        self.events.append(
            MessageEvent(seq=self._next_seq(), at=self.clock.now(), role=role, content=content)
        )

    def tool_call(
        self,
        call_id: str,
        tool: str,
        arguments: dict[str, JsonValue],
        idempotency_key: str | None = None,
    ) -> None:
        """Record a tool invocation. ``arguments`` must be finite, bounded JSON; anything
        else is refused here, not at ``dump_trace``. With a redactor, ``call_id`` and the
        idempotency key are tokenized and every string in ``arguments`` is redacted."""
        if self.redactor is not None:
            call_id = self.redactor.tokenize(call_id)
            arguments = self.redactor.redact_json(arguments)
            if idempotency_key is not None:
                idempotency_key = self.redactor.tokenize(idempotency_key)
        self.events.append(
            ToolCallEvent(
                seq=self._next_seq(),
                at=self.clock.now(),
                call_id=call_id,
                tool=tool,
                arguments=arguments,
                idempotency_key=idempotency_key,
            )
        )

    def tool_result(
        self, call_id: str, ok: bool, result: JsonValue = None, error: Exception | None = None
    ) -> None:
        """Record a tool outcome. A failure must say why; a success must not carry an
        error. A timeout is a failure with an error, not a call left without a result."""
        message = None if error is None else str(error)
        if self.redactor is not None:
            call_id = self.redactor.tokenize(call_id)
            result = self.redactor.redact_json(result)
            message = None if message is None else self.redactor.redact(message)
        self.events.append(
            ToolResultEvent(
                seq=self._next_seq(),
                at=self.clock.now(),
                call_id=call_id,
                ok=ok,
                result=result,
                error=None
                if error is None or message is None
                else ErrorDoc(type=type(error).__name__, message=message),
            )
        )

    # -------------------------------------------------------------- ledger

    def execute(self, command: Command, *, call_id: str | None = None) -> Applied:
        """Run ``command`` against the ledger, recording the command and its outcome.

        On a :class:`LedgerError` the failure is recorded and the error re-raised; the
        ledger is unchanged, because the core never half-applies. With a redactor the
        command is transformed *before* it is recorded or executed, so the recorded
        fingerprints and heads are over the stored form and the trace replays exactly.
        """
        if self.redactor is not None:
            command = self.redactor.command(command)
            if call_id is not None:
                call_id = self.redactor.tokenize(call_id)
        # Exponents travel with the trace, so a consumer that does not bundle this
        # currency can still replay it exactly. A conflicting exponent is refused before
        # anything is recorded: the command could not be written down faithfully.
        self._register_all(command_currencies(command))
        self._commands += 1
        command_id = f"cmd-{self._commands:06d}"
        self.events.append(
            LedgerCommandEvent(
                seq=self._next_seq(),
                at=self.clock.now(),
                command_id=command_id,
                call_id=call_id,
                command=command_doc(command),
            )
        )
        try:
            applied = self.ledger.execute(command, clock=self.clock, ids=self.ids)
        except LedgerError as exc:
            # Not redacted: the command the core saw was already transformed, so its message
            # carries only tokens and operator identifiers, and replay recomputes and
            # compares this exact string.
            self.events.append(
                LedgerResultEvent(
                    seq=self._next_seq(),
                    at=self.clock.now(),
                    command_id=command_id,
                    ok=False,
                    error=ErrorDoc(type=type(exc).__name__, message=str(exc)),
                    head=self.ledger.head,
                    sequence=self.ledger.sequence,
                )
            )
            raise
        self.ledger = applied.ledger
        self.events.append(
            LedgerResultEvent(
                seq=self._next_seq(),
                at=self.clock.now(),
                command_id=command_id,
                ok=True,
                replayed=applied.replayed,
                head=applied.ledger.head,
                sequence=applied.ledger.sequence,
                entry_id=None
                if applied.replayed or applied.entry is None
                else applied.entry.entry_id,
                posted_at=None
                if applied.replayed or applied.entry is None
                else applied.entry.posted_at,
            )
        )
        return applied

    def run(self, commands: Sequence[Command]) -> Ledger:
        """Execute a batch, tolerating ledger errors so the trace records all of them."""
        for command in commands:
            try:
                self.execute(command)
            except LedgerError:
                continue
        return self.ledger

    # --------------------------------------------------------------- output

    def trace(self) -> Trace:
        """The document. With a redactor, ``trace_id`` is tokenized, account names are
        redacted and metadata values are redacted; scenario ids and agent descriptors are
        operator configuration and stay as given."""
        rd = self.redactor
        chart = tuple(AccountDoc.of(a) for a in self.chart.values())
        if rd is not None:
            chart = tuple(a.model_copy(update={"name": rd.redact(a.name)}) for a in chart)
        return Trace(
            schema_version=SCHEMA_VERSION,
            trace_id=self.trace_id if rd is None else rd.tokenize(self.trace_id),
            scenario_id=self.scenario_id,
            agent=self.agent,
            started_at=self._started_at,
            ended_at=self.clock.now(),
            currencies=tuple(CurrencyDoc.of(c) for _, c in sorted(self._currencies.items())),
            chart=chart,
            events=tuple(self.events),
            metadata={k: (v if rd is None else rd.redact(v)) for k, v in self.metadata.items()},
        )
