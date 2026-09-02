# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Re-execute a trace's ledger commands and compare against what it recorded.

This is the mechanism the invariant suite is built on. A trace says what commands were
issued and what the ledger answered. Replay runs the same commands through the pure core,
feeding back the effects the trace recorded (entry ids and timestamps), and reports every
field on which the recorded outcome and the recomputed one disagree. A clean replay means
the trace is internally consistent; a divergence means either the trace or the system
that produced it is lying about something.

Converting a command document into a runtime command can itself fail with a ledger error
(an unbalanced entry, a zero posting, an unknown account currency). That failure *is* the
ledger's verdict on the command and is compared against the recorded result like any
other; replay never crashes on a schema-valid trace.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from ledgergate.ledger import EPOCH, Applied, Ledger, LedgerError
from ledgergate.trace.models import LedgerCommandEvent, LedgerResultEvent, Trace


class _Scripted:
    """A Clock and IdGenerator that hand back exactly what the trace recorded.

    Validation guarantees ``entry_id`` and ``posted_at`` travel together, so one queue of
    pairs suffices. If the ledger asks for more effects than the trace recorded, the two
    already disagree about which commands appended entries; sentinel values make that
    surface as a head divergence rather than a crash.
    """

    def __init__(self) -> None:
        self._pending: deque[tuple[str, datetime]] = deque()
        self._current: tuple[str, datetime] | None = None

    def feed(self, result: LedgerResultEvent) -> None:
        """Arm the effects for exactly one command. Anything left over from a command that
        diverged is discarded, so one divergence does not cascade into the next command."""
        self._pending.clear()
        self._current = None
        if result.entry_id is not None and result.posted_at is not None:
            self._pending.append((result.entry_id, result.posted_at))

    def next_id(self) -> str:
        self._current = self._pending.popleft() if self._pending else ("unrecorded", EPOCH)
        return self._current[0]

    def now(self) -> datetime:
        # The ledger asks for the id first, then the time, for the same entry.
        if self._current is None:
            return EPOCH
        _, at = self._current
        self._current = None
        return at


@dataclass(frozen=True, slots=True)
class Divergence:
    """One field on which the recomputed outcome differs from the recorded one."""

    command_id: str
    seq: int
    field_name: str
    recorded: object
    recomputed: object

    def __str__(self) -> str:
        return (
            f"{self.command_id} (seq {self.seq}): {self.field_name}"
            f" recorded {self.recorded!r}, recomputed {self.recomputed!r}"
        )


@dataclass(frozen=True, slots=True)
class ReplayReport:
    ledger: Ledger
    commands_replayed: int
    divergences: tuple[Divergence, ...] = ()

    @property
    def consistent(self) -> bool:
        return not self.divergences


def replay_trace(trace: Trace) -> ReplayReport:
    """Run every ledger command in ``trace`` and compare against its recorded results.

    Requires the trace to carry a chart of accounts; raises ``ValueError`` otherwise.
    """
    registry = trace.registry()
    ledger = Ledger.empty(trace.chart_of_accounts())
    results = trace.results()
    divergences: list[Divergence] = []
    effects = _Scripted()

    commands = trace.commands()
    for event in commands:
        recorded = results[event.command_id]  # validation guarantees presence
        effects.feed(recorded)

        applied: Applied | None = None
        error: LedgerError | None = None
        try:
            applied = ledger.execute(event.command.to_command(registry), clock=effects, ids=effects)
        except LedgerError as exc:
            error = exc

        divergences.extend(_compare(event, recorded, applied, error, ledger))
        if applied is not None:
            ledger = applied.ledger

    return ReplayReport(ledger, len(commands), tuple(divergences))


def _compare(
    event: LedgerCommandEvent,
    recorded: LedgerResultEvent,
    applied: Applied | None,
    error: LedgerError | None,
    before: Ledger,
) -> list[Divergence]:
    """Every field of the recorded result, against the recomputed outcome. Nothing is skipped.

    Validation has already guaranteed the recorded result has the right shape for its
    ``ok``, so each branch compares a fixed, complete set of fields.
    """

    def diff(name: str, rec: object, got: object) -> Divergence:
        return Divergence(event.command_id, event.seq, name, rec, got)

    out: list[Divergence] = []
    ok = applied is not None
    if recorded.ok != ok:
        out.append(diff("ok", recorded.ok, ok))
        return out  # shapes differ; field-by-field comparison would only add noise

    if applied is None:
        # Both failed. The ledger is unchanged, and the recorded error must be this one.
        assert error is not None and recorded.error is not None
        if recorded.error.type != type(error).__name__:
            out.append(diff("error.type", recorded.error.type, type(error).__name__))
        if recorded.error.message != str(error):
            out.append(diff("error.message", recorded.error.message, str(error)))
        if recorded.head != before.head:
            out.append(diff("head", recorded.head, before.head))
        if recorded.sequence != before.sequence:
            out.append(diff("sequence", recorded.sequence, before.sequence))
        return out

    after = applied.ledger
    if recorded.replayed != applied.replayed:
        out.append(diff("replayed", recorded.replayed, applied.replayed))
    if recorded.head != after.head:
        out.append(diff("head", recorded.head, after.head))
    if recorded.sequence != after.sequence:
        out.append(diff("sequence", recorded.sequence, after.sequence))
    appended = applied.entry if (applied.entry is not None and not applied.replayed) else None
    got_entry_id = None if appended is None else appended.entry_id
    got_posted_at = None if appended is None else appended.posted_at
    if recorded.entry_id != got_entry_id:
        out.append(diff("entry_id", recorded.entry_id, got_entry_id))
    if recorded.posted_at != got_posted_at:
        out.append(diff("posted_at", recorded.posted_at, got_posted_at))
    return out
