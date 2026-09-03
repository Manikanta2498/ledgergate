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

from collections import defaultdict
from itertools import pairwise
from typing import Annotated, Any, Literal

from pydantic import Field, JsonValue, StrictBool, StrictInt, model_validator

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


class InvocationResolution(_V2Event):
    """Exactly one per invocation: what the runtime did with it."""

    type: Literal["invocation_resolution"] = "invocation_resolution"
    intent_id: Identifier
    disposition: Disposition
    operation_id: Identifier | None = None
    outcome_ref: Identifier | None = None
    attempted_digest: Sha256
    presentation_ref: Identifier | None = None

    @model_validator(mode="after")
    def _shape(self) -> InvocationResolution:
        has_op = self.operation_id is not None
        if self.disposition in ("read", "invalid") and has_op:
            raise ValueError(f"{self.disposition} resolution carries no operation")
        if self.disposition in ("new", "replay", "conflict", "approval", "legacy") and not has_op:
            raise ValueError(f"{self.disposition} resolution requires an operation")
        if (self.outcome_ref is not None) != (self.disposition in ("new", "replay", "approval")):
            raise ValueError("outcome_ref is present exactly for new, replay and approval")
        return self


class ApprovalRef(_Strict):
    presentation_ref: Identifier
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
    consumption_ref: Identifier | None = None

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
        for r in resolutions:
            self._check_intent(r, by_intent[r.intent_id])
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

    def _check_ledger_pairs(self) -> None:
        commands = {e.command_id for e in self.events if isinstance(e, LedgerCommandEvent)}
        results = [e.command_id for e in self.events if isinstance(e, LedgerResultEvent)]
        if len(commands) != len(results) or set(results) != commands:
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
            intent_id = f"legacy-{e.command_id}"
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
