# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Re-execute a trace's ledger commands and compare against what it recorded.

This is the mechanism the invariant suite is built on. A trace says what commands were
issued and what the ledger answered. Replay runs the same commands through the pure core,
feeding back the effects the trace recorded (entry ids and timestamps), and reports every
point at which the recorded outcome and the recomputed one disagree. A clean replay
means the trace is internally consistent; a divergence means either the trace or the
system that produced it is lying about something.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ledgergate.ledger import EPOCH, Applied, Ledger, LedgerError
from ledgergate.trace.models import LedgerCommandEvent, LedgerResultEvent, Trace


class _Scripted:
    """A Clock and IdGenerator that hand back exactly what the trace recorded.

    Each call pops the next recorded value. If the trace recorded fewer effects than the
    replay consumes, the ledger and trace already disagree about which commands appended
    entries; the sentinel values make that show up as a head mismatch rather than a
    crash.
    """

    def __init__(self) -> None:
        self.ids: list[str] = []
        self.times: list[datetime] = []

    def feed(self, result: LedgerResultEvent) -> None:
        if result.entry_id is not None and result.posted_at is not None:
            self.ids.append(result.entry_id)
            self.times.append(result.posted_at)

    def next_id(self) -> str:
        return self.ids.pop(0) if self.ids else "unrecorded"

    def now(self) -> datetime:
        return self.times.pop(0) if self.times else EPOCH


@dataclass(frozen=True, slots=True)
class Divergence:
    """One place where the recomputed outcome differs from the recorded one."""

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
    divergences: tuple[Divergence, ...] = field(default=())
    missing_results: tuple[str, ...] = field(default=())

    @property
    def consistent(self) -> bool:
        return not self.divergences and not self.missing_results


def replay_trace(trace: Trace) -> ReplayReport:
    """Run every ledger command in ``trace`` and compare against its recorded results."""
    ledger = Ledger.empty(trace.chart_of_accounts())
    results = trace.results()
    divergences: list[Divergence] = []
    missing: list[str] = []
    effects = _Scripted()

    for event, command in trace.commands():
        recorded = results.get(event.command_id)
        if recorded is None:
            missing.append(event.command_id)
            continue
        effects.feed(recorded)

        applied: Applied | None
        error: LedgerError | None
        try:
            applied, error = ledger.execute(command, clock=effects, ids=effects), None
        except LedgerError as exc:
            applied, error = None, exc

        divergences.extend(_compare(event, recorded, applied, error))
        if applied is not None:
            ledger = applied.ledger

    return ReplayReport(ledger, len(trace.commands()), tuple(divergences), tuple(missing))


def _compare(
    event: LedgerCommandEvent,
    recorded: LedgerResultEvent,
    applied: Applied | None,
    error: LedgerError | None,
) -> list[Divergence]:
    def diff(name: str, rec: object, got: object) -> Divergence:
        return Divergence(event.command_id, event.seq, name, rec, got)

    out: list[Divergence] = []
    ok = applied is not None
    if recorded.ok != ok:
        out.append(diff("ok", recorded.ok, ok))
    if (
        error is not None
        and recorded.error is not None
        and recorded.error.type != type(error).__name__
    ):
        out.append(diff("error.type", recorded.error.type, type(error).__name__))
    if applied is None:
        return out

    after = applied.ledger
    if recorded.replayed is not None and recorded.replayed != applied.replayed:
        out.append(diff("replayed", recorded.replayed, applied.replayed))
    if recorded.head is not None and recorded.head != after.head:
        out.append(diff("head", recorded.head, after.head))
    if recorded.sequence is not None and recorded.sequence != after.sequence:
        out.append(diff("sequence", recorded.sequence, after.sequence))
    appended = applied.entry is not None and not applied.replayed
    if appended and recorded.entry_id is None:
        out.append(diff("entry_id", None, applied.entry.entry_id if applied.entry else None))
    if not appended and recorded.entry_id is not None:
        out.append(diff("entry_id", recorded.entry_id, None))
    return out
