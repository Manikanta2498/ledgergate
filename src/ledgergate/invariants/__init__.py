# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""The invariant registry and scorecard: what a trace must satisfy, and what it did.

An invariant is a pure function of a v2 trace that returns findings. A finding names the
invariant, its severity, the intent it concerns (when one does) and a message. The registry
is the list of invariants a check runs; the scorecard is their combined result, with one
verdict per invariant: ``pass``, ``fail``, or ``no_evidence`` (the trace does not carry what
the invariant would need, which is reported as such and never as a pass).

Every invariant here is grounded in a document: the v2 grammar (``docs/spec/trace-v2.md``),
the journal protocol (``docs/spec/journal.md``) or the ledger core's own rules.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from ledgergate.ledger import LedgerError
from ledgergate.trace.models import LedgerCommandEvent, LedgerResultEvent
from ledgergate.trace.replay import replay_trace
from ledgergate.trace.v2 import PolicyDecision, TraceV2

Severity = Literal["error", "warning"]
Status = Literal["pass", "fail", "no_evidence"]


@dataclass(frozen=True, slots=True)
class Finding:
    invariant: str
    severity: Severity
    message: str
    intent_id: str | None = None


@dataclass(frozen=True, slots=True)
class Invariant:
    name: str
    description: str
    source: str
    check: Callable[[TraceV2], Sequence[Finding]]
    needs: Callable[[TraceV2], bool] = lambda _t: True
    """Whether the trace carries the evidence this invariant needs; if not, ``no_evidence``."""


@dataclass(frozen=True, slots=True)
class InvariantResult:
    name: str
    status: Status
    findings: tuple[Finding, ...] = ()


@dataclass(frozen=True, slots=True)
class Scorecard:
    results: tuple[InvariantResult, ...]
    intents: int
    ledger_commands: int

    @property
    def passed(self) -> bool:
        return all(r.status != "fail" for r in self.results)

    @property
    def failures(self) -> tuple[Finding, ...]:
        return tuple(f for r in self.results for f in r.findings if f.severity == "error")

    def as_json(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "intents": self.intents,
            "ledger_commands": self.ledger_commands,
            "invariants": [
                {
                    "name": r.name,
                    "status": r.status,
                    "findings": [
                        {
                            "severity": f.severity,
                            "intent_id": f.intent_id,
                            "message": f.message,
                        }
                        for f in r.findings
                    ],
                }
                for r in self.results
            ],
        }


# ------------------------------------------------------------------- checks


def _has_runtime_content(t: TraceV2) -> bool:
    return any(r.disposition != "legacy" for r in t.resolutions())


def _has_ledger_pairs(t: TraceV2) -> bool:
    return any(isinstance(e, LedgerCommandEvent) for e in t.events)


def _decided(t: TraceV2) -> dict[str, PolicyDecision]:
    return t.decisions()


def denied_never_reaches_ledger(t: TraceV2) -> list[Finding]:
    """A denied or approval-pending intent has no ledger pair (trace-v2 grammar, ordinal 4)."""
    out = []
    owners = _pair_owners(t)
    for iid, d in _decided(t).items():
        if d.decision != "allow" and iid in owners:
            out.append(
                Finding(
                    "denied_never_reaches_ledger",
                    "error",
                    f"{iid}: decision {d.decision} yet a ledger command was recorded",
                    iid,
                )
            )
    return out


def replay_never_reevaluates(t: TraceV2) -> list[Finding]:
    """A replay or conflict carries no policy decision and no ledger pair."""
    out = []
    decided = _decided(t)
    owners = _pair_owners(t)
    for r in t.resolutions():
        if r.disposition in ("replay", "conflict") and (
            r.intent_id in decided or r.intent_id in owners
        ):
            out.append(
                Finding(
                    "replay_never_reevaluates",
                    "error",
                    f"{r.intent_id}: {r.disposition} carries policy evidence or a ledger pair",
                    r.intent_id,
                )
            )
    return out


def every_write_was_decided(t: TraceV2) -> list[Finding]:
    """Every ``new`` or ``approval`` intent has exactly one policy decision."""
    decided = _decided(t)
    return [
        Finding(
            "every_write_was_decided",
            "error",
            f"{r.intent_id}: {r.disposition} without a policy decision",
            r.intent_id,
        )
        for r in t.resolutions()
        if r.disposition in ("new", "approval") and r.intent_id not in decided
    ]


def runtime_decisions_are_verdicts(t: TraceV2) -> list[Finding]:
    """A ``runtime.``-prefixed decision is a deny with a failed approval verdict as reason, on an
    ``approval`` intent, and carries a presentation reference."""
    out = []
    by_id = {r.intent_id: r for r in t.resolutions()}
    for iid, d in _decided(t).items():
        if not d.runtime_written:
            continue
        r = by_id[iid]
        ok = (
            d.decision == "deny"
            and r.disposition == "approval"
            and d.approval is not None
            and d.approval.verdict == d.reason
            and d.approval.verdict not in ("approval_valid", "approval_not_applicable")
        )
        if not ok:
            out.append(
                Finding(
                    "runtime_decisions_are_verdicts",
                    "error",
                    f"{iid}: runtime-written decision is not a failed-verdict deny on an approval",
                    iid,
                )
            )
    return out


def context_matches_decision(t: TraceV2) -> list[Finding]:
    """The persisted context agrees with the decision row about the approval verdict, and on a
    failed verdict carries no policy-derived subject or aggregates (no policy code ran)."""
    out = []
    for iid, d in _decided(t).items():
        ctx_approval = d.context.get("approval")
        recorded = None if d.approval is None else d.approval.verdict
        ctx_verdict = None if not isinstance(ctx_approval, dict) else ctx_approval.get("verdict")
        if ctx_verdict != recorded:
            out.append(
                Finding(
                    "context_matches_decision",
                    "error",
                    f"{iid}: context verdict {ctx_verdict!r} differs from decision {recorded!r}",
                    iid,
                )
            )
        if d.runtime_written and (
            d.context.get("subject") is not None or d.context.get("aggregates")
        ):
            out.append(
                Finding(
                    "context_matches_decision",
                    "error",
                    f"{iid}: runtime decision carries policy-derived context",
                    iid,
                )
            )
        if d.context.get("policy_set_version") != d.policy_set_version:
            out.append(
                Finding(
                    "context_matches_decision",
                    "error",
                    f"{iid}: context names a different policy set than the decision",
                    iid,
                )
            )
    return out


def ledger_pairs_replay(t: TraceV2) -> list[Finding]:
    """Re-executing every ledger command reproduces every recorded result, head and entry."""
    report = replay_trace(t.ledger_view())
    return [
        Finding("ledger_pairs_replay", "error", str(d), _owner_of(t, d.command_id))
        for d in report.divergences
    ]


def books_balance_and_chain_verifies(t: TraceV2) -> list[Finding]:
    """The replayed ledger's trial balance balances and its hash chain verifies."""
    report = replay_trace(t.ledger_view())
    out = []
    if not report.ledger.trial_balance().is_balanced:
        out.append(
            Finding("books_balance_and_chain_verifies", "error", "trial balance does not balance")
        )
    try:
        report.ledger.verify_chain()
    except LedgerError as exc:
        out.append(Finding("books_balance_and_chain_verifies", "error", f"hash chain: {exc}"))
    return out


def legacy_carries_no_policy_evidence(t: TraceV2) -> list[Finding]:
    """Lifted v1 content never carries a policy decision (an invented allow is forbidden)."""
    decided = _decided(t)
    return [
        Finding(
            "legacy_carries_no_policy_evidence",
            "error",
            f"{r.intent_id}: legacy intent carries a policy decision",
            r.intent_id,
        )
        for r in t.resolutions()
        if r.disposition == "legacy" and r.intent_id in decided
    ]


def _pair_owners(t: TraceV2) -> dict[str, str]:
    owners: dict[str, str] = {}
    current: str | None = None
    for e in t.events:
        iid = getattr(e, "intent_id", None)
        if iid is not None:
            current = iid
        elif isinstance(e, LedgerCommandEvent) and current is not None:
            owners[current] = e.command_id
    return owners


def _owner_of(t: TraceV2, cid: str) -> str | None:
    return next((iid for iid, c in _pair_owners(t).items() if c == cid), None)


REGISTRY: tuple[Invariant, ...] = (
    Invariant(
        "denied_never_reaches_ledger",
        denied_never_reaches_ledger.__doc__ or "",
        "docs/spec/trace-v2.md, event grammar",
        denied_never_reaches_ledger,
        _has_runtime_content,
    ),
    Invariant(
        "replay_never_reevaluates",
        replay_never_reevaluates.__doc__ or "",
        "docs/spec/trace-v2.md, replay and conflict",
        replay_never_reevaluates,
        _has_runtime_content,
    ),
    Invariant(
        "every_write_was_decided",
        every_write_was_decided.__doc__ or "",
        "docs/spec/journal.md, write protocol step 7",
        every_write_was_decided,
        _has_runtime_content,
    ),
    Invariant(
        "runtime_decisions_are_verdicts",
        runtime_decisions_are_verdicts.__doc__ or "",
        "docs/spec/journal.md, approval artefacts",
        runtime_decisions_are_verdicts,
        _has_runtime_content,
    ),
    Invariant(
        "context_matches_decision",
        context_matches_decision.__doc__ or "",
        "docs/spec/trace-v2.md, policy_decision payload",
        context_matches_decision,
        lambda t: bool(t.decisions()),
    ),
    Invariant(
        "ledger_pairs_replay",
        ledger_pairs_replay.__doc__ or "",
        "ledger core: determinism",
        ledger_pairs_replay,
        _has_ledger_pairs,
    ),
    Invariant(
        "books_balance_and_chain_verifies",
        books_balance_and_chain_verifies.__doc__ or "",
        "ledger core: double entry and hash chain",
        books_balance_and_chain_verifies,
        _has_ledger_pairs,
    ),
    Invariant(
        "legacy_carries_no_policy_evidence",
        legacy_carries_no_policy_evidence.__doc__ or "",
        "docs/spec/trace-v2.md, legacy grammar",
        legacy_carries_no_policy_evidence,
        lambda t: any(r.disposition == "legacy" for r in t.resolutions()),
    ),
)


def check(trace: TraceV2, registry: Sequence[Invariant] = REGISTRY) -> Scorecard:
    """Run every invariant; report ``no_evidence`` where the trace cannot support one."""
    results = []
    for inv in registry:
        if not inv.needs(trace):
            results.append(InvariantResult(inv.name, "no_evidence"))
            continue
        findings = tuple(inv.check(trace))
        status: Status = "fail" if any(f.severity == "error" for f in findings) else "pass"
        results.append(InvariantResult(inv.name, status, findings))
    return Scorecard(
        tuple(results),
        intents=len(trace.resolutions()),
        ledger_commands=sum(isinstance(e, LedgerResultEvent) for e in trace.events),
    )


__all__ = [
    "REGISTRY",
    "Finding",
    "Invariant",
    "InvariantResult",
    "Scorecard",
    "check",
]
