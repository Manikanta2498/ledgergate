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
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, JsonValue, StrictBool, StrictInt, model_validator

from ledgergate.codec import digest
from ledgergate.ledger import CURRENCIES, ChartOfAccounts
from ledgergate.trace.models import (
    AccountDoc,
    AgentDoc,
    CommandDoc,
    CurrencyCode,
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


class ContextApproval(_Strict):
    presentation: Annotated[StrictInt, Field(ge=1)]
    verdict: Verdict


DecimalText = Annotated[str, Field(pattern=r"^-?[0-9]{1,40}$")]
AggregateName = Annotated[str, Field(pattern=r"^applied\.[a-z_]+\.[A-Z]{3}\.[1-9][0-9]*s$")]


class PolicyContextDoc(_Strict):
    """The persisted ``PolicyContext``, field for field. Nothing else is a context."""

    principal: Identifier
    subject: Identifier | None = None
    command_digest: Sha256
    digest_kind: Literal["fingerprint", "request"]
    evaluated_at: Timestamp
    policy_set_version: Identifier
    command_kind: Identifier | None = None
    amount: DecimalText | None = None
    currency: CurrencyCode | None = None
    aggregates: dict[AggregateName, DecimalText] = Field(default_factory=dict)
    approval: ContextApproval | None = None


RUNTIME_RULES = frozenset({"runtime.approval_rejected"})
"""The only rules the runtime itself writes. A ``runtime.``-prefixed rule outside this set
is not a decision the journal can have made."""


class PolicyDecision(_V2Event):
    """Carries the inputs (the whole serialized ``PolicyContext``), not a summary."""

    type: Literal["policy_decision"] = "policy_decision"
    intent_id: Identifier
    policy_set_version: Identifier
    decision: DecisionKind
    matched_rule: ShortText
    reason: ShortText
    context: PolicyContextDoc
    approval: ApprovalRef | None = None
    consumption_ref: ConsumptionRef | None = None

    @model_validator(mode="after")
    def _shape(self) -> PolicyDecision:
        if self.matched_rule.startswith("runtime.") and self.matched_rule not in RUNTIME_RULES:
            raise ValueError(f"{self.matched_rule!r} is not a rule the runtime writes")
        if self.context.policy_set_version != self.policy_set_version:
            raise ValueError("context names a different policy set than the decision")
        ctx = self.context.approval
        recorded = None if self.approval is None else self.approval.verdict
        if (None if ctx is None else ctx.verdict) != recorded:
            raise ValueError("context and decision disagree about the approval verdict")
        if (
            ctx is not None
            and self.approval is not None
            and f"presentation-{ctx.presentation}" != self.approval.presentation_ref
        ):
            raise ValueError("context and decision name different presentations")
        return self

    @property
    def runtime_written(self) -> bool:
        return self.matched_rule.startswith("runtime.")


class ApprovalPresentation(_V2Event):
    """The presentation row: one per artefact presented, carrying the pure-check result and,
    once the signature verified, the approver's identity fields. Nothing of an unverified
    artefact but its fixed-grammar bindings is here, as in the journal."""

    type: Literal["approval_presentation"] = "approval_presentation"
    intent_id: Identifier
    presentation_ref: PresentationRef
    verified: StrictBool
    check_result: Literal[
        "checks_passed",
        "approval_invalid",
        "approval_expired",
        "approval_scope_mismatch",
        "approval_not_applicable",
    ]
    journal_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    fingerprint: Sha256
    issued_at: Timestamp
    expires_at: Timestamp
    approval_id: Identifier | None = None
    approver: Identifier | None = None

    @model_validator(mode="after")
    def _shape(self) -> ApprovalPresentation:
        if self.verified != (self.approval_id is not None and self.approver is not None):
            raise ValueError("identity fields are present exactly when the signature verified")
        if self.check_result == "approval_invalid" and self.verified:
            raise ValueError("an invalid signature cannot be verified")
        if not self.verified and self.check_result not in (
            "approval_invalid",
            "approval_not_applicable",
        ):
            raise ValueError("expiry and scope are checked only after the signature verified")
        return self


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
    | ApprovalPresentation
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
    "approval_presentation": 3,
    "policy_decision": 4,
    "ledger_command": 5,
    "ledger_result": 6,
    "read_result": 7,
    "tool_result": 8,
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
    policy_config_digest: Sha256 | Literal["none"] = "none"
    policy_configuration: dict[str, JsonValue] | None = None
    """The set's declarative rules, when it has any: what a verifier recomputes decisions
    from. Its JCS digest is ``policy_config_digest``, the value the journal's definition
    recorded and compared at every open."""
    events: Annotated[tuple[V2Event, ...], Field(max_length=5_000_000)]
    metadata: StringMap = Field(default_factory=dict)

    # ------------------------------------------------------------ grammar

    @model_validator(mode="after")
    def _grammar(self) -> TraceV2:
        if self.ended_at < self.started_at:
            raise ValueError("ended_at precedes started_at")
        if any(e.seq != i + 1 for i, e in enumerate(self.events)):
            raise ValueError("event seq must be dense from 1")
        if self.policy_configuration is not None:
            from ledgergate.codec import digest

            if digest(self.policy_configuration) != self.policy_config_digest:
                raise ValueError("policy_config_digest is not the digest of policy_configuration")
        for e in self.events:
            if isinstance(e, PolicyDecision) and e.policy_set_version != self.policy_set_version:
                raise ValueError(
                    f"{e.intent_id}: decision names a policy set other than the trace's"
                )
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
        if derived and (self.chart is None or self.currencies is None):
            # The definition always has both; without them the ledger rows could not run and
            # the document would certify less than the journal recorded.
            raise ValueError("a journal-derived trace carries its chart and currencies")
        if derived and self.policy_set_version != "none" and self.policy_config_digest == "none":
            raise ValueError("a derived trace under a policy set carries its config digest")
        if not derived and len(legacy) != len(resolutions):
            raise ValueError("a lifted document carries only legacy dispositions")
        if not derived:
            self._check_v1_pairing()
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
        presentations = [e for e in group if isinstance(e, ApprovalPresentation)]
        if len(presentations) != (0 if r.presentation_ref is None else 1):
            raise ValueError(
                f"{r.intent_id}: one approval_presentation iff an artefact was presented"
            )
        if presentations and presentations[0].presentation_ref != r.presentation_ref:
            raise ValueError(f"{r.intent_id}: presentation event names another presentation")
        decision_event = next((e for e in group if isinstance(e, PolicyDecision)), None)
        if presentations and decision_event is not None and decision_event.approval is None:
            raise ValueError(f"{r.intent_id}: a decision after a presentation carries its verdict")
        if decision_event is not None and decision_event.approval is not None and not presentations:
            raise ValueError(f"{r.intent_id}: a decision names a presentation the intent lacks")
        pairs = kinds.count("ledger_command")
        reads = kinds.count("read_result")
        if d == "invalid":
            if intents or decisions or pairs or reads or presentations:
                raise ValueError(f"{r.intent_id}: invalid carries no intent, decision or result")
            if r.presentation_ref is not None:
                raise ValueError(f"{r.intent_id}: an invalid invocation presents nothing")
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
        operation_key: dict[str, str] = {}
        for r in resolutions:
            group = by_intent[r.intent_id]
            intent = next((e for e in group if isinstance(e, CommandIntent | LegacyIntent)), None)
            if intent is None:
                continue
            if isinstance(intent, LegacyIntent):
                # A lifted command may be one the core refuses to construct (v1 records the
                # rejection); its digest is over the document, which is always computable.
                if (
                    digest(intent.command.model_dump(mode="json", exclude_none=True))
                    != r.attempted_digest
                ):
                    raise ValueError(f"{r.intent_id}: attempted_digest is not the command's digest")
            else:
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
            assert isinstance(intent, CommandIntent)
            key = intent.command.key
            if r.disposition == "new":
                operation_fp[op] = fp
                operation_key[op] = key
            elif operation_key.get(op) != key:
                # The fingerprint excludes the key by design; the key is what selected the
                # operation, so every later intent against it carries the same one.
                raise ValueError(f"{r.intent_id}: {r.disposition} carries another operation's key")
            if r.disposition == "conflict":
                if operation_fp.get(op) == fp:
                    raise ValueError(
                        f"{r.intent_id}: a conflict's command must differ from the operation's"
                    )
            elif r.disposition != "new" and operation_fp.get(op) != fp:
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
        self._check_call_binds_intent(r, call, group)
        return {id(call), id(result)}

    def _check_call_binds_intent(
        self, r: InvocationResolution, call: ToolCallEvent, group: list[AnyV2Event]
    ) -> None:
        """The boundary call is the intent: an invalid call carries the empty arguments, no
        key and no tool the journal admitted; a read call carries the read's tool and
        arguments; a write call decodes, with its idempotency key, to the intent's command."""
        if r.disposition == "invalid":
            if call.arguments or call.idempotency_key is not None:
                raise ValueError(f"{r.intent_id}: an invalid call carries no arguments or key")
            return
        intent = group[0]
        if isinstance(intent, ReadIntent):
            if call.tool != intent.tool or call.arguments != intent.arguments:
                raise ValueError(f"{r.intent_id}: tool_call differs from the read intent")
            if call.idempotency_key is not None:
                raise ValueError(f"{r.intent_id}: a read carries no idempotency key")
            return
        assert isinstance(intent, CommandIntent)
        if call.idempotency_key is None:
            raise ValueError(f"{r.intent_id}: a write call carries its idempotency key")
        if call.tool != intent.command.kind:
            raise ValueError(f"{r.intent_id}: tool_call tool differs from the command kind")
        from ledgergate.codec import CodecError, decode_command

        try:
            rebuilt = decode_command(
                {"kind": call.tool, "key": call.idempotency_key, **call.arguments}, self.registry()
            )
        except (CodecError, ValueError) as exc:
            raise ValueError(f"{r.intent_id}: tool_call arguments do not decode: {exc}") from exc
        if rebuilt != intent.command.to_command(self.registry()):
            raise ValueError(f"{r.intent_id}: tool_call arguments are not the intent's command")

    def _check_v1_pairing(self) -> None:
        """A lifted document keeps v1's boundary discipline: call ids unique, one result per
        call, after it, nothing orphaned."""
        calls = [e for e in self.events if isinstance(e, ToolCallEvent)]
        results = [e for e in self.events if isinstance(e, ToolResultEvent)]
        call_seq = {c.call_id: c.seq for c in calls}
        if len(call_seq) != len(calls):
            raise ValueError("tool_call ids must be unique")
        result_seq = {x.call_id: x.seq for x in results}
        if len(result_seq) != len(results):
            raise ValueError("each tool_call may have only one tool_result")
        if orphans := sorted(result_seq.keys() - call_seq.keys()):
            raise ValueError(f"tool_result without a call: {orphans}")
        if unanswered := sorted(call_seq.keys() - result_seq.keys()):
            raise ValueError(f"tool_call without a result: {unanswered}")
        if early := sorted(c for c, s in call_seq.items() if result_seq[c] <= s):
            raise ValueError(f"tool_result precedes its call: {early}")

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
                        attempted_digest=digest(
                            e.command.model_dump(mode="json", exclude_none=True)
                        ),
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
        ended_at=trace.ended_at
        if trace.ended_at is not None
        else max((e.at for e in trace.events), default=trace.started_at),
        currencies=trace.currencies,
        chart=trace.chart,
        policy_set_version="legacy",
        events=tuple(events),
        metadata=dict(trace.metadata),
    )


def json_schema() -> dict[str, Any]:
    """The published schema: the model's, with every discriminator and ``schema_version``
    required, so a document a consumer validates against the schema is one the model
    accepts and the reverse (pydantic emits defaults for these, which would let the schema
    accept a document the runtime cannot route)."""
    schema = TraceV2.model_json_schema()
    for name, definition in [("TraceV2", schema), *schema.get("$defs", {}).items()]:
        props = definition.get("properties", {})
        required = set(definition.get("required", []))
        for field_name in ("type", "schema_version"):
            if field_name in props:
                props[field_name].pop("default", None)
                required.add(field_name)
        if required:
            definition["required"] = sorted(required)
        del name
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/Manikanta2498/ledgergate/schema/trace/v2.json",
        **schema,
    }


__all__ = [
    "SCHEMA_VERSION_2",
    "AnyV2Event",
    "ApprovalPresentation",
    "ApprovalRef",
    "CommandIntent",
    "Disposition",
    "ErrorDoc",
    "InvocationResolution",
    "LegacyIntent",
    "Payload",
    "PolicyContextDoc",
    "PolicyDecision",
    "ReadIntent",
    "ReadResult",
    "StrictBool",
    "TraceV2",
    "V2Event",
    "json_schema",
    "lift",
]
