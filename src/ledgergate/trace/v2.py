# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Trace schema v2: intents and dispositions, per ``docs/spec/trace-v2.md``.

v1 (:mod:`ledgergate.trace.models`) is frozen and remains the offline ingest format. v2 is
what the runtime derives from the journal: every invocation yields exactly one
``invocation_resolution`` saying what the runtime did (its *disposition*), an intent if
admission succeeded, a ``policy_decision`` only when policy actually ran, and a ledger pair
only when the decision was ``allow`` on a write. A v1 document is *lifted* into v2 under its
own ``legacy`` grammar, which synthesizes neither tool events nor policy evidence.

The grammar's cardinality and order rules are enforced by :class:`TraceV2`'s validator, as
v1's are by :class:`~ledgergate.trace.models.Trace`.
"""

from __future__ import annotations

import re
from collections import defaultdict
from itertools import pairwise
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, JsonValue, StrictBool, StrictInt, model_validator

from ledgergate.ledger import CURRENCIES, ChartOfAccounts
from ledgergate.trace.models import (
    AccountDoc,
    AgentDoc,
    CommandDoc,
    CurrencyDoc,
    ErrorDoc,
    Identifier,
    LedgerCommandEvent,
    LedgerResultEvent,
    MessageEvent,
    Payload,
    Registry,
    Sha256,
    ShortText,
    StringMap,
    Timestamp,
    ToolCallEvent,
    ToolResultEvent,
    Trace,
    _Strict,
)

SCHEMA_VERSION_2: Literal["2"] = "2"

Disposition = Literal["new", "replay", "conflict", "approval", "read", "invalid", "legacy"]
DecisionKind = Literal["allow", "deny", "approval_required"]
Verdict = Literal[
    "approval_valid",
    "approval_already_used",
    "approval_not_applicable",
    "approval_invalid",
    "approval_expired",
    "approval_scope_mismatch",
]


class _V2Event(_Strict):
    seq: Annotated[StrictInt, Field(ge=1)]
    at: Timestamp


class CommandIntent(_V2Event):
    type: Literal["command_intent"] = "command_intent"
    intent_id: Identifier
    call_id: Identifier
    command: CommandDoc


class ReadIntent(_V2Event):
    type: Literal["read_intent"] = "read_intent"
    intent_id: Identifier
    call_id: Identifier
    tool: Identifier
    arguments: dict[str, JsonValue]


class LegacyIntent(_V2Event):
    """A v1 ledger command lifted on read. Carries no policy evidence, by design."""

    type: Literal["legacy_intent"] = "legacy_intent"
    intent_id: Identifier
    call_id: Identifier | None = None
    command: CommandDoc


OutcomeRef = Annotated[str, Field(pattern=r"^outcome-[1-9][0-9]*$")]
PresentationRef = Annotated[str, Field(pattern=r"^presentation-[1-9][0-9]*$")]
ConsumptionRef = Annotated[str, Field(pattern=r"^consumption-[1-9][0-9]*$")]


def _ref_number(ref: str) -> int:
    return int(ref.rsplit("-", 1)[1])


class InvocationResolution(_V2Event):
    """Exactly one per invocation: what the runtime did with it."""

    type: Literal["invocation_resolution"] = "invocation_resolution"
    intent_id: Identifier
    disposition: Disposition
    operation_id: Identifier | None = None
    outcome_ref: OutcomeRef | None = None
    attempted_digest: Sha256
    presentation_ref: PresentationRef | None = None

    @model_validator(mode="after")
    def _shape(self) -> InvocationResolution:
        if self.disposition == "approval" and self.presentation_ref is None:
            raise ValueError("an approval disposition is defined by a presented artefact")
        has_op = self.operation_id is not None
        if self.disposition in ("read", "invalid") and has_op:
            raise ValueError(f"{self.disposition} resolution carries no operation")
        if self.disposition in ("new", "replay", "conflict", "approval", "legacy") and not has_op:
            raise ValueError(f"{self.disposition} resolution requires an operation")
        if (self.outcome_ref is not None) != (self.disposition in ("new", "replay", "approval")):
            raise ValueError("outcome_ref is present exactly for new, replay and approval")
        return self


class ApprovalRef(_Strict):
    presentation_ref: PresentationRef
    verdict: Verdict


class PolicyDecision(_V2Event):
    """Carries the inputs (the whole serialized ``PolicyContext``), not a summary."""

    type: Literal["policy_decision"] = "policy_decision"
    intent_id: Identifier
    policy_set_version: Identifier
    decision: DecisionKind
    matched_rule: ShortText
    reason: ShortText
    context: dict[str, JsonValue]
    approval: ApprovalRef | None = None
    consumption_ref: ConsumptionRef | None = None

    @property
    def runtime_written(self) -> bool:
        return self.matched_rule.startswith("runtime.")


class ReadResult(_V2Event):
    type: Literal["read_result"] = "read_result"
    intent_id: Identifier
    cursor: Annotated[StrictInt, Field(ge=0)]
    head: Sha256
    result_digest: Sha256


AnyV2Event = (
    MessageEvent
    | ToolCallEvent
    | ToolResultEvent
    | CommandIntent
    | ReadIntent
    | LegacyIntent
    | InvocationResolution
    | PolicyDecision
    | LedgerCommandEvent
    | LedgerResultEvent
    | ReadResult
)
V2Event = Annotated[AnyV2Event, Field(discriminator="type")]

_ORDINAL: dict[str, int] = {
    "tool_call": 0,
    "command_intent": 1,
    "read_intent": 1,
    "invocation_resolution": 2,
    "policy_decision": 3,
    "ledger_command": 4,
    "ledger_result": 5,
    "read_result": 6,
    "tool_result": 7,
}


class TraceV2(_Strict):
    """LedgerGate trace, schema v2. Consumers enforce, as this model does: every intent has
    exactly one invocation_resolution; an intent event exists iff the disposition is not
    invalid and its kind follows the disposition (read_intent for read, legacy_intent for
    legacy, command_intent otherwise); a policy_decision exists exactly once for new and
    approval, never for replay, conflict or legacy, at most once for read; a ledger pair
    exists iff the decision was allow on new or approval, exactly once for legacy, never
    otherwise; a read_result exists iff a read was not denied; every runtime intent is
    bracketed by a tool_call immediately before and a tool_result immediately after with
    the same call_id; events of one intent appear in ordinal order; seq is dense and
    strictly increasing; every ledger_command has exactly one ledger_result and command_id
    is unique; every operation and outcome reference resolves to one recorded earlier, a
    replay or failed approval names the operation's outcome current at the time, an
    approval is against a pending operation and a replay with a presentation never is, a
    produced outcome is produced once, in allocation order; a command intent's fingerprint
    is its attempted_digest, matches the operation's for new, replay and approval and
    differs for conflict, and equals its
    ledger_command's, whose command_id is the operation and whose call_id is the intent's;
    in a derived document every boundary event brackets an intent; a document is derived iff
    it carries a journal_id, and then has no legacy resolution, or lifted, and then has no
    journal_id and only legacy resolutions; derived
    references (outcome, presentation, consumption) follow their fixed grammars; in a
    derived document intent and operation ids follow theirs, intent numbers strictly
    increase, an intent's events are contiguous, and every row an invocation wrote has a
    sequence strictly between its own and the next invocation's."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "$comment": "See the TraceV2 docstring for the grammar consumers enforce."
        },
    )
    schema_version: Literal["2"] = SCHEMA_VERSION_2
    trace_id: Identifier
    journal_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None = None
    scenario_id: Identifier | None = None
    agent: AgentDoc | None = None
    started_at: Timestamp
    ended_at: Timestamp
    currencies: Annotated[tuple[CurrencyDoc, ...], Field(max_length=1000)] | None = None
    chart: Annotated[tuple[AccountDoc, ...], Field(max_length=10000)] | None = None
    policy_set_version: Identifier
    events: Annotated[tuple[V2Event, ...], Field(max_length=200000)]
    metadata: StringMap = Field(default_factory=dict)

    # ------------------------------------------------------------ grammar

    @model_validator(mode="after")
    def _grammar(self) -> TraceV2:
        if self.ended_at < self.started_at:
            raise ValueError("ended_at precedes started_at")
        if any(b.seq <= a.seq for a, b in pairwise(self.events)):
            raise ValueError("event seq must be strictly increasing")
        by_intent: dict[str, list[AnyV2Event]] = defaultdict(list)
        resolutions = [e for e in self.events if isinstance(e, InvocationResolution)]
        ids = [r.intent_id for r in resolutions]
        if len(ids) != len(set(ids)):
            raise ValueError("each intent has exactly one invocation_resolution")
        # Ledger pairs carry no intent_id (their shape is v1's). A ledger_command belongs to
        # the intent whose events immediately precede it in anchored order; its
        # ledger_result follows it by command_id, however far away the result sits.
        current: str | None = None
        pair_owner: dict[str, str] = {}
        for e in self.events:
            intent_id = getattr(e, "intent_id", None)
            if intent_id is not None:
                current = intent_id
                by_intent[intent_id].append(e)
            elif isinstance(e, LedgerCommandEvent):
                if current is None:
                    raise ValueError(f"ledger_command {e.command_id} precedes any intent")
                pair_owner[e.command_id] = current
                by_intent[current].append(e)
            elif isinstance(e, LedgerResultEvent):
                owner = pair_owner.get(e.command_id)
                if owner is None:
                    raise ValueError(f"ledger_result {e.command_id} has no ledger_command")
                by_intent[owner].append(e)
        # A document is either lifted (every resolution legacy, no journal) or derived (no
        # legacy at all); the two grammars never mix, since neither producer mixes them.
        # The partition is by producer, not by content: a derived document carries a
        # journal_id and no legacy resolution; a lifted one carries no journal_id and only
        # legacy resolutions (possibly none: a v1 document may hold tool events alone).
        legacy = [r for r in resolutions if r.disposition == "legacy"]
        derived = self.journal_id is not None
        if derived and legacy:
            raise ValueError("a journal-derived trace carries no legacy content")
        if not derived and len(legacy) != len(resolutions):
            raise ValueError("a lifted document carries only legacy dispositions")
        positions = {id(e): i for i, e in enumerate(self.events)}
        bracketing: set[int] = set()
        for r in resolutions:
            self._check_intent(r, by_intent[r.intent_id])
            if r.disposition != "legacy":
                bracketing.update(self._check_boundary(r, by_intent[r.intent_id], positions))
        if derived:
            # In a runtime trace every boundary event brackets an intent; a stray pair would
            # be a call the journal never saw.
            for e in self.events:
                if isinstance(e, ToolCallEvent | ToolResultEvent) and id(e) not in bracketing:
                    raise ValueError(f"{e.type} {e.call_id!r} brackets no intent")
        self._check_references(resolutions, by_intent)
        self._check_commands(resolutions, by_intent)
        if derived:
            self._check_windows(resolutions, by_intent)
        for intent_id, group in by_intent.items():
            if not any(isinstance(e, InvocationResolution) for e in group):
                raise ValueError(f"intent {intent_id!r} has events but no resolution")
        self._check_ledger_pairs()
        return self

    def _check_intent(self, r: InvocationResolution, group: list[AnyV2Event]) -> None:
        kinds = [e.type for e in group]
        d = r.disposition
        intents = [k for k in kinds if k in ("command_intent", "read_intent", "legacy_intent")]
        decisions = kinds.count("policy_decision")
        pairs = kinds.count("ledger_command")
        reads = kinds.count("read_result")
        if d == "invalid":
            if intents or decisions or pairs or reads:
                raise ValueError(f"{r.intent_id}: invalid carries no intent, decision or result")
            return
        if len(intents) != 1:
            raise ValueError(f"{r.intent_id}: exactly one intent for disposition {d}")
        expected_intent = {"read": "read_intent", "legacy": "legacy_intent"}.get(
            d, "command_intent"
        )
        if intents[0] != expected_intent:
            raise ValueError(f"{r.intent_id}: disposition {d} requires {expected_intent}")
        if d in ("replay", "conflict", "legacy") and decisions:
            raise ValueError(f"{r.intent_id}: {d} never carries a policy_decision")
        if d in ("new", "approval") and decisions != 1:
            raise ValueError(f"{r.intent_id}: {d} carries exactly one policy_decision")
        if d == "read" and decisions > 1:
            raise ValueError(f"{r.intent_id}: a read carries at most one policy_decision")
        decision = next((e for e in group if isinstance(e, PolicyDecision)), None)
        if (
            decision is not None
            and decision.decision == "approval_required"
            and d in ("read", "approval")
        ):
            raise ValueError(f"{r.intent_id}: {d} never awaits approval (a configuration fault)")
        allowed = decision is not None and decision.decision == "allow"
        if d in ("new", "approval"):
            if pairs != (1 if allowed else 0):
                raise ValueError(f"{r.intent_id}: ledger pair iff the decision was allow")
        elif d == "legacy":
            if pairs != 1:
                raise ValueError(f"{r.intent_id}: legacy carries exactly one ledger pair")
        elif pairs:
            raise ValueError(f"{r.intent_id}: {d} carries no ledger pair")
        denied = decision is not None and decision.decision == "deny"
        if d == "read":
            if reads != (0 if denied else 1):
                raise ValueError(f"{r.intent_id}: read_result iff the read was not denied")
        elif reads:
            raise ValueError(f"{r.intent_id}: only a read carries a read_result")
        if d != "legacy":
            ordinals = [_ORDINAL[k] for k in kinds]
            if ordinals != sorted(ordinals):
                raise ValueError(f"{r.intent_id}: events out of ordinal order")
        elif [k for k in kinds if k != "ledger_result"] != [
            "legacy_intent",
            "invocation_resolution",
            "ledger_command",
        ]:
            raise ValueError(f"{r.intent_id}: legacy grammar is intent, resolution, command")

    def _check_references(
        self, resolutions: list[InvocationResolution], by_intent: dict[str, list[AnyV2Event]]
    ) -> None:
        """Every reference resolves to something recorded earlier: a new operation and a
        produced outcome are fresh and in allocation order; a replay, conflict or approval
        names an operation an earlier new created; a replay or failed approval names the
        outcome that was that operation's current one at the time, and a failed approval's
        was pending."""
        operations: set[str] = set()
        outcomes: dict[str, str] = {}  # outcome_ref -> operation_id
        current: dict[str, str] = {}  # operation_id -> its latest produced outcome
        pending: dict[str, bool] = {}  # operation_id -> latest outcome awaits approval
        last_produced = 0
        for r in resolutions:
            op, out = r.operation_id, r.outcome_ref
            decision = next(
                (e for e in by_intent[r.intent_id] if isinstance(e, PolicyDecision)), None
            )
            produced = r.disposition == "new" or (
                r.disposition == "approval"
                and decision is not None
                and not decision.runtime_written
            )
            if r.disposition in ("read", "invalid", "legacy"):
                continue
            assert op is not None
            if r.disposition == "new":
                if op in operations:
                    raise ValueError(f"{r.intent_id}: new names an operation already created")
                operations.add(op)
            elif op not in operations:
                raise ValueError(f"{r.intent_id}: {r.disposition} names an unknown operation")
            if r.disposition == "approval" and not pending.get(op, False):
                # An approval, whatever its verdict, is against an operation whose current
                # outcome is pending; a terminal operation has nothing left to approve.
                raise ValueError(f"{r.intent_id}: approval against a non-pending operation")
            if r.disposition == "replay" and r.presentation_ref is not None and pending.get(op):
                # Same fingerprint, pending operation, artefact presented: the runtime's
                # disposition is approval, never replay (journal write step 4; a conflict is
                # decided on the fingerprint before the artefact is considered).
                raise ValueError(
                    f"{r.intent_id}: an artefact against a pending operation is an approval"
                )
            if produced:
                assert out is not None
                if out in outcomes:
                    raise ValueError(f"{r.intent_id}: outcome {out} was already produced")
                number = _ref_number(out)
                if number <= last_produced:
                    raise ValueError(f"{r.intent_id}: outcome {out} is out of allocation order")
                last_produced = number
                outcomes[out] = op
                current[op] = out
                assert decision is not None
                pending[op] = decision.decision == "approval_required"
            elif out is not None:
                # A replay, or a failed-verdict approval, names the operation's *current*
                # outcome at the time: the latest one produced before this resolution.
                if current.get(op) != out:
                    raise ValueError(f"{r.intent_id}: outcome {out} was not {op}'s current outcome")

    def _check_windows(
        self, resolutions: list[InvocationResolution], by_intent: dict[str, list[AnyV2Event]]
    ) -> None:
        """In a derived document every reference is anchored to the invocation that wrote it:
        intent numbers strictly increase along the trace; every row an invocation wrote (its
        operation if new, its produced outcome, its presentation, its consumption) has a
        sequence strictly between the invocation's and the next invocation's, except the
        operation of a new, which is allocated just before its invocation."""
        numbers = []
        for r in resolutions:
            if not re.fullmatch(r"intent-[1-9][0-9]*", r.intent_id):
                raise ValueError(f"{r.intent_id}: a derived intent id is intent-<sequence>")
            if r.operation_id is not None and not re.fullmatch(
                r"command-[1-9][0-9]*", r.operation_id
            ):
                raise ValueError(f"{r.intent_id}: a derived operation id is command-<sequence>")
            numbers.append(_ref_number(r.intent_id))
        if numbers != sorted(set(numbers)):
            raise ValueError("derived intent numbers must strictly increase along the trace")
        for i, r in enumerate(resolutions):
            lo = numbers[i]
            hi = numbers[i + 1] if i + 1 < len(numbers) else float("inf")
            decision = next(
                (e for e in by_intent[r.intent_id] if isinstance(e, PolicyDecision)), None
            )
            produced = r.disposition == "new" or (
                r.disposition == "approval"
                and decision is not None
                and not decision.runtime_written
            )
            written: list[str | None] = [r.presentation_ref]
            if r.disposition == "new":
                # The operations row is the first row of a new operation's transaction,
                # allocated just before the invocation row that references it.
                prev = numbers[i - 1] if i else 0
                assert r.operation_id is not None
                if not (prev < _ref_number(r.operation_id) < lo):
                    raise ValueError(
                        f"{r.intent_id}: {r.operation_id} was not created by this invocation"
                    )
            if produced:
                written.append(r.outcome_ref)
            if decision is not None:
                written.append(decision.consumption_ref)
            for ref in written:
                if ref is not None and not (lo < _ref_number(ref) < hi):
                    raise ValueError(
                        f"{r.intent_id}: {ref} was not written by this invocation's transaction"
                    )

    def _check_commands(
        self, resolutions: list[InvocationResolution], by_intent: dict[str, list[AnyV2Event]]
    ) -> None:
        """The command an intent carries is the one its digest, its operation and its ledger
        pair are about: the fingerprint of command_intent.command equals attempted_digest; a
        new, replay or approval matches the operation's fingerprint and a conflict differs;
        the owning ledger_command carries the same command."""
        from ledgergate.ledger import command_fingerprint

        registry = self.registry()
        operation_fp: dict[str, str] = {}
        for r in resolutions:
            group = by_intent[r.intent_id]
            intent = next((e for e in group if isinstance(e, CommandIntent | LegacyIntent)), None)
            if intent is None:
                continue
            fp = command_fingerprint(intent.command.to_command(registry))
            if fp != r.attempted_digest:
                raise ValueError(
                    f"{r.intent_id}: attempted_digest is not the command's fingerprint"
                )
            op = r.operation_id
            assert op is not None
            pair = next((e for e in group if isinstance(e, LedgerCommandEvent)), None)
            if pair is not None:
                if pair.command != intent.command:
                    raise ValueError(
                        f"{r.intent_id}: ledger_command differs from the intent's command"
                    )
                if pair.command_id != op or pair.call_id != getattr(intent, "call_id", None):
                    raise ValueError(
                        f"{r.intent_id}: ledger_command names another operation or call"
                    )
            if r.disposition == "legacy":
                continue
            if r.disposition == "new":
                operation_fp[op] = fp
            elif r.disposition == "conflict":
                if operation_fp.get(op) == fp:
                    raise ValueError(
                        f"{r.intent_id}: a conflict's command must differ from the operation's"
                    )
            elif operation_fp.get(op) != fp:
                raise ValueError(
                    f"{r.intent_id}: {r.disposition} carries a command other than the operation's"
                )

    def _check_boundary(
        self, r: InvocationResolution, group: list[AnyV2Event], positions: dict[int, int]
    ) -> set[int]:
        """A runtime intent is bracketed by its own tool_call and tool_result: the event
        immediately before its first event is a tool_call, the event immediately after its
        last is a tool_result, and both carry the intent's call_id."""
        first, last = group[0], group[-1]
        before = positions[id(first)] - 1
        after = positions[id(last)] + 1
        if after - before - 1 != len(group):
            raise ValueError(f"{r.intent_id}: another event is interleaved within the intent")
        call = self.events[before] if before >= 0 else None
        result = self.events[after] if after < len(self.events) else None
        if not isinstance(call, ToolCallEvent):
            raise ValueError(f"{r.intent_id}: no tool_call immediately before the intent")
        if not isinstance(result, ToolResultEvent) or result.call_id != call.call_id:
            raise ValueError(f"{r.intent_id}: no matching tool_result immediately after")
        call_id = getattr(first, "call_id", None)
        if call_id is not None and call_id != call.call_id:
            raise ValueError(f"{r.intent_id}: intent call_id differs from its tool_call")
        return {id(call), id(result)}

    def _check_ledger_pairs(self) -> None:
        commands = [e.command_id for e in self.events if isinstance(e, LedgerCommandEvent)]
        results = [e.command_id for e in self.events if isinstance(e, LedgerResultEvent)]
        if len(set(commands)) != len(commands):
            raise ValueError("command_id must be unique across ledger_command events")
        if sorted(results) != sorted(commands):
            raise ValueError("every ledger_command has exactly one ledger_result")

    # ------------------------------------------------------------ helpers

    def registry(self) -> Registry:
        out = dict(CURRENCIES)
        out.update((c.code, c.to_currency()) for c in self.currencies or [])
        return out

    def chart_of_accounts(self) -> ChartOfAccounts:
        if self.chart is None:
            raise ValueError("trace carries no chart")
        return ChartOfAccounts(a.to_account(self.registry()) for a in self.chart)

    def resolutions(self) -> tuple[InvocationResolution, ...]:
        return tuple(e for e in self.events if isinstance(e, InvocationResolution))

    def decisions(self) -> dict[str, PolicyDecision]:
        return {e.intent_id: e for e in self.events if isinstance(e, PolicyDecision)}

    def ledger_view(self) -> Trace:
        """The ledger pairs alone as a v1 document, so v1's replayer re-executes them.
        Nothing else of v2 is representable in v1, and nothing else replays."""
        pairs = tuple(
            e.model_copy(update={"call_id": None}) if isinstance(e, LedgerCommandEvent) else e
            for e in self.events
            if isinstance(e, LedgerCommandEvent | LedgerResultEvent)
        )  # call references point at boundary events this view deliberately omits
        return Trace(
            trace_id=self.trace_id,
            agent=self.agent or AgentDoc(name="derived"),
            started_at=self.started_at,
            ended_at=self.ended_at,
            currencies=self.currencies,
            chart=self.chart,
            events=pairs,
            metadata={},
        )


# ------------------------------------------------------------------------ lift


def lift(trace: Trace) -> TraceV2:
    """Lift a v1 document into v2 under the ``legacy`` grammar: no boundary event and no
    policy evidence is invented. Ordering is anchored to the v1 ``seq``."""
    anchored: list[tuple[tuple[int, int], Any]] = []
    for e in trace.events:
        if isinstance(e, LedgerCommandEvent):
            # v1 command ids may use the whole identifier length; the lifted id is bounded by
            # position, which cannot overflow, and the command_id itself stays on the pair.
            intent_id = f"legacy-{e.seq}"
            anchored.append(
                (
                    (e.seq, 0),
                    LegacyIntent(
                        seq=1, at=e.at, intent_id=intent_id, call_id=e.call_id, command=e.command
                    ),
                )
            )
            anchored.append(
                (
                    (e.seq, 1),
                    InvocationResolution(
                        seq=1,
                        at=e.at,
                        intent_id=intent_id,
                        disposition="legacy",
                        operation_id=e.command_id,
                        attempted_digest=_fingerprint_of(e.command, trace.registry()),
                    ),
                )
            )
            anchored.append(((e.seq, 2), e))
        else:
            anchored.append(((e.seq, 0), e))
    events = [
        ev.model_copy(update={"seq": i + 1})
        for i, (_k, ev) in enumerate(sorted(anchored, key=lambda x: x[0]))
    ]
    return TraceV2(
        trace_id=trace.trace_id,
        scenario_id=trace.scenario_id,
        agent=trace.agent,
        started_at=trace.started_at,
        ended_at=trace.ended_at,
        currencies=trace.currencies,
        chart=trace.chart,
        policy_set_version="legacy",
        events=tuple(events),
        metadata=dict(trace.metadata),
    )


def _fingerprint_of(command: Any, registry: Registry) -> str:
    from ledgergate.ledger import command_fingerprint

    return command_fingerprint(command.to_command(registry))


__all__ = [
    "SCHEMA_VERSION_2",
    "AnyV2Event",
    "ApprovalRef",
    "CommandIntent",
    "Disposition",
    "ErrorDoc",
    "InvocationResolution",
    "LegacyIntent",
    "Payload",
    "PolicyDecision",
    "ReadIntent",
    "ReadResult",
    "StrictBool",
    "TraceV2",
    "V2Event",
    "lift",
]
