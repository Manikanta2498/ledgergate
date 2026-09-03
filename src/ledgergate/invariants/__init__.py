# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""The invariant registry and scorecard: what a trace must satisfy, and what it did.

An invariant is a pure function of a v2 trace that returns findings. A finding names the
invariant, its severity, the intent it concerns (when one does) and a message. The registry
is the list of invariants a check runs; the scorecard is their combined result, with one
verdict per invariant: ``pass``, ``fail``, or ``no_evidence`` (the trace does not carry what
the invariant would need, which is reported as such and never as a pass).

Every invariant here is grounded in a document: the v2 grammar (``docs/spec/trace-v2.md``),
the journal protocol (``docs/spec/journal.md``) or the ledger core's own rules. Several
restate rules the :class:`~ledgergate.trace.v2.TraceV2` validator also enforces at load; a
document violating them fails to load rather than failing here, and the scorecard row then
records that the loaded trace satisfies them. The registry is the statement of what is
checked; the validator is one of the mechanisms.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from ledgergate.ledger import GENESIS_HASH, LedgerError
from ledgergate.trace.models import LedgerCommandEvent, LedgerResultEvent, ToolResultEvent
from ledgergate.trace.replay import replay_trace
from ledgergate.trace.v2 import (
    InvocationResolution,
    PolicyDecision,
    ReadResult,
    TraceV2,
    _ref_number,
)

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
    """Admitted invocations: an ``invalid`` invocation yields a resolution but no intent."""
    ledger_commands: int

    @property
    def status(self) -> Status:
        """``fail`` if any invariant failed; ``pass`` only if none failed *and at least one
        ran*; otherwise ``no_evidence``: nothing was checked, and that is never a pass."""
        if any(r.status == "fail" for r in self.results):
            return "fail"
        if any(r.status == "pass" for r in self.results):
            return "pass"
        return "no_evidence"

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    @property
    def failures(self) -> tuple[Finding, ...]:
        return tuple(f for r in self.results for f in r.findings if f.severity == "error")

    def as_json(self) -> dict[str, object]:
        return {
            "status": self.status,
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


def _has_disposition(*kinds: str) -> Callable[[TraceV2], bool]:
    return lambda t: any(r.disposition in kinds for r in t.resolutions())


def _has_non_allow_decision(t: TraceV2) -> bool:
    return any(d.decision != "allow" for d in t.decisions().values())


def _has_ledger_pairs(t: TraceV2) -> bool:
    """Replay needs both the pairs and the chart they were posted against."""
    return t.chart is not None and any(isinstance(e, LedgerCommandEvent) for e in t.events)


def _decided(t: TraceV2) -> dict[str, PolicyDecision]:
    return t.decisions()


def denied_never_reaches_ledger(t: TraceV2) -> list[Finding]:
    """A denied or approval-pending intent has no ledger pair, and once an operation was
    denied no later ledger command is about it: a denied command never reaches the ledger."""
    out = []
    owners = _pair_owners(t)
    by_id = {r.intent_id: r for r in t.resolutions()}
    denied_ops: set[str] = set()
    for e in t.events:
        if isinstance(e, PolicyDecision):
            iid = e.intent_id
            if e.decision != "allow" and iid in owners:
                out.append(
                    Finding(
                        "denied_never_reaches_ledger",
                        "error",
                        f"{iid}: decision {e.decision} yet a ledger command was recorded",
                        iid,
                    )
                )
            # A runtime deny on a failed verdict leaves the operation pending; only the policy
            # set's own deny is terminal for the operation.
            if e.decision == "deny" and not e.runtime_written and by_id[iid].operation_id:
                denied_ops.add(by_id[iid].operation_id or "")
        elif isinstance(e, LedgerCommandEvent) and e.command_id in denied_ops:
            out.append(
                Finding(
                    "denied_never_reaches_ledger",
                    "error",
                    f"{e.command_id}: a ledger command for an operation that was denied",
                    _owner_of(t, e.command_id),
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
    """Over every decision: it references the presentation its own invocation made and none
    otherwise; a ``runtime.``-prefixed decision is a deny on an ``approval`` intent with a
    failed verdict as its reason and no consumption; every failed verdict was decided by the
    runtime; an approval the policy set decided consumed a valid artefact, and no other
    disposition carries a verdict but not-applicable; a consumption is kept exactly for a
    valid verdict, recorded after its presentation, and each presentation and consumption is
    referenced by at most one decision."""
    out = []
    by_id = {r.intent_id: r for r in t.resolutions()}
    failed = {
        "approval_already_used",
        "approval_invalid",
        "approval_expired",
        "approval_scope_mismatch",
    }
    seen_presentations: set[str] = set()
    seen_consumptions: set[str] = set()
    for iid, d in _decided(t).items():
        verdict = None if d.approval is None else d.approval.verdict
        r = by_id[iid]
        for ref, seen, what in (
            (
                None if d.approval is None else d.approval.presentation_ref,
                seen_presentations,
                "presentation",
            ),
            (d.consumption_ref, seen_consumptions, "consumption"),
        ):
            if ref is not None:
                if ref in seen:
                    out.append(
                        Finding(
                            "runtime_decisions_are_verdicts",
                            "error",
                            f"{iid}: {what} {ref} is referenced by more than one decision",
                            iid,
                        )
                    )
                seen.add(ref)
        # A decision references the presentation its own invocation made, and none otherwise.
        mine = None if d.approval is None else d.approval.presentation_ref
        if mine != r.presentation_ref:
            out.append(
                Finding(
                    "runtime_decisions_are_verdicts",
                    "error",
                    f"{iid}: decision presentation {mine!r} differs from the invocation's"
                    f" {r.presentation_ref!r}",
                    iid,
                )
            )
        # The verdict fits the disposition: an approval that the policy set decided consumed a
        # valid artefact; any other disposition can only have found an artefact not applicable.
        if (
            r.disposition == "approval"
            and not d.runtime_written
            and (verdict != "approval_valid" or d.consumption_ref is None)
        ):
            out.append(
                Finding(
                    "runtime_decisions_are_verdicts",
                    "error",
                    f"{iid}: an approval the policy set decided must have consumed a valid"
                    " artefact",
                    iid,
                )
            )
        if r.disposition != "approval" and verdict not in (None, "approval_not_applicable"):
            out.append(
                Finding(
                    "runtime_decisions_are_verdicts",
                    "error",
                    f"{iid}: verdict {verdict} is only reachable on an approval disposition",
                    iid,
                )
            )
        if (
            d.consumption_ref is not None
            and d.approval is not None
            and _ref_number(d.consumption_ref) <= _ref_number(d.approval.presentation_ref)
        ):
            out.append(
                Finding(
                    "runtime_decisions_are_verdicts",
                    "error",
                    f"{iid}: a consumption is recorded after its presentation",
                    iid,
                )
            )
        if d.runtime_written and d.approval is None:
            out.append(
                Finding(
                    "runtime_decisions_are_verdicts",
                    "error",
                    f"{iid}: a runtime-written decision must carry the verdict it decided on",
                    iid,
                )
            )
        if (d.consumption_ref is not None) != (verdict == "approval_valid"):
            out.append(
                Finding(
                    "runtime_decisions_are_verdicts",
                    "error",
                    f"{iid}: a consumption is kept exactly for a valid verdict",
                    iid,
                )
            )
        if verdict in failed and not d.runtime_written:
            out.append(
                Finding(
                    "runtime_decisions_are_verdicts",
                    "error",
                    f"{iid}: a failed verdict must be decided by the runtime, not the policy set",
                    iid,
                )
            )
        if not d.runtime_written:
            continue
        ok = (
            d.decision == "deny"
            and r.disposition == "approval"
            and d.approval is not None
            and d.approval.verdict == d.reason
            and d.approval.verdict not in ("approval_valid", "approval_not_applicable")
            and d.consumption_ref is None
            and d.approval.presentation_ref == r.presentation_ref
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
    """The persisted context agrees with the decision row about the approval verdict, names the
    same policy set, was computed over the digest the resolution attempted, and on a failed
    verdict carries no policy-derived subject or aggregates (no policy code ran)."""
    out = []
    attempted = {r.intent_id: r.attempted_digest for r in t.resolutions()}
    kinds = {
        r.intent_id: ("request" if r.disposition == "read" else "fingerprint")
        for r in t.resolutions()
    }
    for iid, d in _decided(t).items():
        if d.context.get("digest_kind") != kinds.get(iid):
            out.append(
                Finding(
                    "context_matches_decision",
                    "error",
                    f"{iid}: digest_kind {d.context.get('digest_kind')!r} does not fit"
                    " the disposition",
                    iid,
                )
            )
        if d.context.get("command_digest") != attempted.get(iid):
            out.append(
                Finding(
                    "context_matches_decision",
                    "error",
                    f"{iid}: context digest differs from the resolution's attempted digest",
                    iid,
                )
            )
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
    owners = _pair_owners(t)
    return [
        Finding("ledger_pairs_replay", "error", str(d), _owner_of(t, d.command_id, owners))
        for d in report.divergences
    ]


def books_balance_and_chain_verifies(t: TraceV2) -> list[Finding]:
    """The *recorded* heads form one chain: each ledger_result's head equals the previous
    recorded head unless it appended an entry, and the ledger replayed from the pairs balances
    and verifies its own chain (a core self-check the recorded chain must agree with)."""
    out = []
    previous = GENESIS_HASH
    for e in t.events:
        if isinstance(e, LedgerResultEvent) and e.head is not None:
            appended = e.ok and e.entry_id is not None
            if not appended and e.head != previous:
                out.append(
                    Finding(
                        "books_balance_and_chain_verifies",
                        "error",
                        f"{e.command_id}: head moved without an entry being appended",
                    )
                )
            previous = e.head
    report = replay_trace(t.ledger_view())
    if not report.ledger.trial_balance().is_balanced:
        out.append(
            Finding("books_balance_and_chain_verifies", "error", "trial balance does not balance")
        )
    try:
        report.ledger.verify_chain()
    except LedgerError as exc:
        out.append(Finding("books_balance_and_chain_verifies", "error", f"hash chain: {exc}"))
    return out


def read_observed_the_recorded_head(t: TraceV2) -> list[Finding]:
    """Every read_result's head is the head the most recent preceding ledger_result recorded
    (or the genesis hash), and its cursor equals the largest outcome any earlier resolution
    referenced (every outcome is named by the resolution that produced it, which precedes
    any later read): a read saw the projection the journal was at, neither stale nor ahead."""
    out = []
    head = GENESIS_HASH
    max_outcome = 0
    for e in t.events:
        if isinstance(e, LedgerResultEvent) and e.head is not None:
            head = e.head
        elif isinstance(e, InvocationResolution) and e.outcome_ref is not None:
            max_outcome = max(max_outcome, _ref_number(e.outcome_ref))
        elif isinstance(e, ReadResult):
            if e.head != head:
                out.append(
                    Finding(
                        "read_observed_the_recorded_head",
                        "error",
                        f"{e.intent_id}: read saw head {e.head[:12]}"
                        f" but the books were at {head[:12]}",
                        e.intent_id,
                    )
                )
            if e.cursor != max_outcome:
                out.append(
                    Finding(
                        "read_observed_the_recorded_head",
                        "error",
                        f"{e.intent_id}: read cursor {e.cursor} is not the latest outcome"
                        f" recorded before it ({max_outcome})",
                        e.intent_id,
                    )
                )
    return out


def _tool_results(t: TraceV2) -> dict[str, ToolResultEvent]:
    """The tool_result that closes each runtime intent's bracket (the event after its last)."""
    out: dict[str, ToolResultEvent] = {}
    last: dict[str, int] = {}
    for i, e in enumerate(t.events):
        iid = getattr(e, "intent_id", None)
        if iid is not None:
            last[iid] = i
    result_at = {
        e.command_id: i for i, e in enumerate(t.events) if isinstance(e, LedgerResultEvent)
    }
    for iid, cid in _pair_owners(t).items():
        if cid in result_at:
            last[iid] = max(last[iid], result_at[cid])
    for iid, i in last.items():
        nxt = t.events[i + 1] if i + 1 < len(t.events) else None
        if isinstance(nxt, ToolResultEvent):
            out[iid] = nxt
    return out


def caller_was_told_what_happened(t: TraceV2) -> list[Finding]:
    """The tool_result closing each runtime intent says what the journal did (the two
    decision-to-outcome tables): success iff a read was not denied or the ledger applied;
    otherwise the error type of the path taken (AdmissionError, IdempotencyConflictError,
    PolicyDenied, ApprovalRequired, ApprovalRejected, or the core's own error); a denial
    carries the decision's rule and reason; an applied write's served head, sequence and entry
    are the ledger result's; and a replay is told exactly what the producing invocation was
    told (the same result with ``replayed`` set, or the same error verbatim)."""
    out = []
    results = _tool_results(t)
    decisions = _decided(t)
    ledger_results = {e.command_id: e for e in t.events if isinstance(e, LedgerResultEvent)}
    owners = _pair_owners(t)
    producer_of: dict[str, str] = {}
    for r in t.resolutions():
        if r.disposition == "legacy":
            continue
        tr = results.get(r.intent_id)
        if tr is None:
            out.append(
                Finding(
                    "caller_was_told_what_happened",
                    "error",
                    f"{r.intent_id}: no tool_result",
                    r.intent_id,
                )
            )
            continue
        d = decisions.get(r.intent_id)
        expected_ok: bool
        expected_error: str | None
        if r.disposition == "invalid":
            expected_ok, expected_error = False, "AdmissionError"
        elif r.disposition == "conflict":
            expected_ok, expected_error = False, "IdempotencyConflictError"
        elif r.disposition == "read":
            denied = d is not None and d.decision == "deny"
            expected_ok, expected_error = not denied, "PolicyDenied" if denied else None
            if denied:
                assert d is not None
                message = None if tr.error is None else tr.error.message
                if message != f"{d.matched_rule}: {d.reason}":
                    out.append(
                        Finding(
                            "caller_was_told_what_happened",
                            "error",
                            f"{r.intent_id}: denial message does not carry the decision's rule"
                            " and reason",
                            r.intent_id,
                        )
                    )
        elif r.disposition == "replay":
            producer = producer_of.get(r.outcome_ref or "")
            told = results.get(producer or "")
            if told is None:
                out.append(
                    Finding(
                        "caller_was_told_what_happened",
                        "error",
                        f"{r.intent_id}: replay of an outcome no earlier invocation was told about",
                        r.intent_id,
                    )
                )
                continue
            expected_ok = told.ok
            expected_error = None if told.error is None else told.error.type
            # Exactly what the producer was told: the same result with replayed set, or the
            # same error verbatim (journal step 6).
            exact = (
                tr.result == {**told.result, "replayed": True}
                if told.ok and isinstance(told.result, dict)
                else (tr.ok == told.ok and tr.result == told.result and tr.error == told.error)
            )
            if not exact:
                out.append(
                    Finding(
                        "caller_was_told_what_happened",
                        "error",
                        f"{r.intent_id}: replay was not told exactly what {producer} was told",
                        r.intent_id,
                    )
                )
        else:  # new, approval
            assert d is not None
            if r.outcome_ref is not None and (d.decision != "deny" or not d.runtime_written):
                producer_of[r.outcome_ref] = r.intent_id
            if d.decision == "deny":
                expected_ok = False
                expected_error = "ApprovalRejected" if d.runtime_written else "PolicyDenied"
            elif d.decision == "approval_required":
                expected_ok, expected_error = False, "ApprovalRequired"
            if d.decision != "allow":
                message = None if tr.error is None else tr.error.message
                if message != f"{d.matched_rule}: {d.reason}":
                    out.append(
                        Finding(
                            "caller_was_told_what_happened",
                            "error",
                            f"{r.intent_id}: denial message does not carry the decision's rule"
                            " and reason",
                            r.intent_id,
                        )
                    )
            else:
                lr = ledger_results.get(owners.get(r.intent_id, ""))
                if lr is None:
                    out.append(
                        Finding(
                            "caller_was_told_what_happened",
                            "error",
                            f"{r.intent_id}: allowed write without a ledger result",
                            r.intent_id,
                        )
                    )
                    continue
                expected_ok = lr.ok
                expected_error = None if lr.error is None else lr.error.type
                served = tr.result if isinstance(tr.result, dict) else {}
                if lr.ok and (
                    served.get("head") != lr.head
                    or served.get("sequence") != lr.sequence
                    or served.get("entry_id") != lr.entry_id
                ):
                    out.append(
                        Finding(
                            "caller_was_told_what_happened",
                            "error",
                            f"{r.intent_id}: served head, sequence or entry differ from the"
                            " ledger result",
                            r.intent_id,
                        )
                    )
        actual_error = None if tr.error is None else tr.error.type
        if tr.ok != expected_ok or actual_error != expected_error:
            out.append(
                Finding(
                    "caller_was_told_what_happened",
                    "error",
                    f"{r.intent_id}: caller was told ok={tr.ok} {actual_error or ''} but the"
                    f" journal did ok={expected_ok} {expected_error or ''}",
                    r.intent_id,
                )
            )
    return out


def read_result_binds_the_served_value(t: TraceV2) -> list[Finding]:
    """A read_result's digest is the JCS digest of the value the caller was served in the
    tool_result, so the served value is bound to the row (agreement of that value with the
    replayed books is not checked here; the head and cursor checks cover the position)."""
    from ledgergate.codec import digest

    out = []
    results = _tool_results(t)
    for e in t.events:
        if isinstance(e, ReadResult):
            tr = results.get(e.intent_id)
            served = None if tr is None else tr.result
            if tr is None or digest(served) != e.result_digest:
                out.append(
                    Finding(
                        "read_result_binds_the_served_value",
                        "error",
                        f"{e.intent_id}: served value does not match the read's digest",
                        e.intent_id,
                    )
                )
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


def _owner_of(t: TraceV2, cid: str, owners: dict[str, str] | None = None) -> str | None:
    owners = _pair_owners(t) if owners is None else owners
    return next((iid for iid, c in owners.items() if c == cid), None)


REGISTRY: tuple[Invariant, ...] = (
    Invariant(
        "denied_never_reaches_ledger",
        denied_never_reaches_ledger.__doc__ or "",
        "docs/spec/trace-v2.md, event grammar",
        denied_never_reaches_ledger,
        _has_non_allow_decision,
    ),
    Invariant(
        "replay_never_reevaluates",
        replay_never_reevaluates.__doc__ or "",
        "docs/spec/trace-v2.md, replay and conflict",
        replay_never_reevaluates,
        _has_disposition("replay", "conflict"),
    ),
    Invariant(
        "every_write_was_decided",
        every_write_was_decided.__doc__ or "",
        "docs/spec/journal.md, write protocol step 7",
        every_write_was_decided,
        _has_disposition("new", "approval"),
    ),
    Invariant(
        "runtime_decisions_are_verdicts",
        runtime_decisions_are_verdicts.__doc__ or "",
        "docs/spec/journal.md, approval artefacts",
        runtime_decisions_are_verdicts,
        lambda t: bool(t.decisions()),
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
        "read_observed_the_recorded_head",
        read_observed_the_recorded_head.__doc__ or "",
        "docs/spec/journal.md, read protocol",
        read_observed_the_recorded_head,
        lambda t: any(isinstance(e, ReadResult) for e in t.events),
    ),
    Invariant(
        "caller_was_told_what_happened",
        caller_was_told_what_happened.__doc__ or "",
        "docs/spec/journal.md, decision-to-outcome tables",
        caller_was_told_what_happened,
        lambda t: any(r.disposition != "legacy" for r in t.resolutions()),
    ),
    Invariant(
        "read_result_binds_the_served_value",
        read_result_binds_the_served_value.__doc__ or "",
        "docs/spec/journal.md, read protocol (result_digest)",
        read_result_binds_the_served_value,
        lambda t: any(isinstance(e, ReadResult) for e in t.events),
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
        intents=sum(r.disposition != "invalid" for r in trace.resolutions()),
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
