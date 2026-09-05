# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""The policy seam: an explicit, serializable context and a pure decision.

A policy set is a deterministic, versioned function of a :class:`PolicyContext`. The
journal builds the context inside the admitting transaction, reading whatever historical
aggregates the set declares it needs through :class:`History`, persists the whole context
with the decision, and carries it verbatim into the v2 ``policy_decision`` event, so a
consumer with the same set can recompute the decision from the recorded inputs.

M2b shipped the null set (version ``none``). M3 adds :class:`ThresholdPolicySet`, a
declarative set over amounts and per-subject windows, which is what most deployments
need: refunds above a line need a human, refunds above another line are refused, and a
subject cannot receive more than a cap within a window however the requests are split.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from ledgergate.ledger import (
    Advance,
    Command,
    Money,
    OpenTransaction,
    Post,
    Refund,
    Reverse,
)

DecisionKind = Literal["allow", "deny", "approval_required"]
DigestKind = Literal["fingerprint", "request"]


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """Everything a policy may look at. Persisted verbatim with every decision.

    ``subject`` is derived by the policy set per intent kind (``None`` under the null
    set); ``amount`` is the command's single amount as a decimal string, or ``None`` for
    commands without one (``post``, ``reverse``, lifecycle-only ``advance``). Aggregates
    are the historical values the rules read, as decimal strings, so a decision replays
    without live state. ``approval`` is the verdict on a presented artefact, if any.
    """

    principal: str
    subject: str | None
    command_digest: str
    digest_kind: DigestKind
    evaluated_at: datetime
    policy_set_version: str
    command_kind: str | None = None
    amount: str | None = None
    currency: str | None = None
    aggregates: dict[str, Any] = field(default_factory=dict)
    approval: dict[str, Any] | None = None

    def serialized(self) -> dict[str, Any]:
        doc = asdict(self)
        doc["evaluated_at"] = self.evaluated_at.isoformat()
        return doc


@dataclass(frozen=True, slots=True)
class Decision:
    decision: DecisionKind
    matched_rule: str
    reason: str


class History(Protocol):
    """What a policy set may read about the past, inside the admitting transaction."""

    def applied_total(self, *, subject: str, kind: str, currency: str, since: datetime) -> int:
        """Sum of minor units applied for ``subject`` by commands of ``kind`` in
        ``currency`` whose producing invocation (the ``new`` or ``approval`` one) was
        requested at or after ``since``; that ``requested_at`` is the window's time base."""
        ...


class PolicySet(Protocol):
    @property
    def version(self) -> str: ...

    def configuration_digest(self) -> str:
        """A digest of the rules themselves. The definition records it and ``open`` compares
        it, so two processes with the same version label and different rules cannot share
        a journal; the label alone is operator-typed and binds nothing."""
        ...

    def configuration(self) -> Mapping[str, Any] | None:
        """The declarative rules as a JSON document a consumer can recompute decisions
        from, or ``None`` for a set whose rules are code: then a trace carries only the
        digest and a verifier reports the recomputation as ``no_evidence``."""
        ...

    def gates_read(self, tool: str) -> bool: ...

    def subject_of(self, command: Command) -> str | None:
        """How this set derives the subject from a command, per intent kind."""
        ...

    def aggregates_for(self, command: Command, now: datetime, history: History) -> dict[str, Any]:
        """The historical values the rules will read; every amount a decimal string."""
        ...

    def evaluate(self, context: PolicyContext) -> Decision: ...


MAX_WINDOW_SECONDS = (
    10**9
)  # ~31 years; a window beyond it is a configuration fault, not an overflow


def _whole(value: Any) -> int:
    """A configuration amount or window: an integer, or a decimal string of one. A float or a
    fractional value is refused rather than truncated (minor units and seconds are whole)."""
    if isinstance(value, bool | float):
        raise ValueError(f"expected a whole number, got {value!r}")
    return int(value)


def _bounded_window(seconds: int) -> int:
    if not 0 < seconds <= MAX_WINDOW_SECONDS:
        raise ValueError(f"window must be within 1..{MAX_WINDOW_SECONDS} seconds, got {seconds}")
    return seconds


def set_name(kind: type) -> str:
    """The module-qualified class name a configuration records: two sets with one name in
    two modules are two sets."""
    return f"{kind.__module__}.{kind.__qualname__}"


AMOUNT_KINDS = frozenset({"open_transaction", "refund"})
"""The command kinds that carry one amount a threshold can govern."""


def command_amount(command: Command) -> Money | None:
    match command:
        case OpenTransaction(_, _, amount):
            return amount
        case Refund(_, _, money, _):
            return money
        case _:
            return None


def command_kind(command: Command) -> str:
    return {
        Post: "post",
        Reverse: "reverse",
        OpenTransaction: "open_transaction",
        Advance: "advance",
        Refund: "refund",
    }[type(command)]


class NullPolicySet:
    """Allows everything, gates nothing, derives no subject, reads no history."""

    version = "none"

    def configuration(self) -> Mapping[str, Any] | None:
        # The module-qualified class name is part of the configuration, as for every set: a
        # subclass with another name cannot share a journal defined under the null set.
        return {"set": set_name(type(self)), "version": self.version}

    def configuration_digest(self) -> str:
        from ledgergate.codec import digest

        return digest(self.configuration())

    def gates_read(self, tool: str) -> bool:
        return False

    def subject_of(self, command: Command) -> str | None:
        return None

    def aggregates_for(self, command: Command, now: datetime, history: History) -> dict[str, Any]:
        return {}

    def evaluate(self, context: PolicyContext) -> Decision:
        return Decision("allow", "none.allow_all", "null policy set: no rules configured")


@dataclass(frozen=True, slots=True)
class Threshold:
    """A monetary line for one command kind in one currency, in minor units."""

    kind: str
    currency: str
    amount: int


@dataclass(frozen=True, slots=True)
class WindowCap:
    """A per-subject cap: ``kind`` commands for one subject may not exceed ``amount``
    minor units of ``currency`` within the trailing ``window``."""

    kind: str
    currency: str
    amount: int
    window: timedelta


@dataclass(frozen=True)
class ThresholdPolicySet:
    """Declarative rules over amounts and per-subject windows.

    Rules are evaluated in a fixed order and the first that fires decides:

    1. ``deny_above``: an amount strictly above the line is denied.
    2. ``window_caps``: the subject's applied total in the window plus this amount above
       the cap is denied.
    3. ``approve_above``: an amount strictly above the line requires approval, unless a
       valid approval is in the context, in which case it is allowed.
    4. otherwise allow.

    The subject of a transaction command is its (tokenized) ``transaction_id``; ``post``
    and ``reverse`` have no subject and no amount, so only rule 4 can apply to them.
    """

    version: str
    deny_above: Sequence[Threshold] = ()
    approve_above: Sequence[Threshold] = ()
    window_caps: Sequence[WindowCap] = ()
    gated_reads: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        from ledgergate.ledger.identifiers import require_identifier

        require_identifier(self.version, "policy set version")
        if self.version == "none":
            raise ValueError("'none' names the null policy set")
        # Copy caller-owned sequences so the rules that were digested are the rules that run.
        object.__setattr__(self, "deny_above", tuple(self.deny_above))
        object.__setattr__(self, "approve_above", tuple(self.approve_above))
        object.__setattr__(self, "window_caps", tuple(self.window_caps))
        object.__setattr__(self, "gated_reads", frozenset(self.gated_reads))
        lines: list[Threshold | WindowCap] = [
            *self.deny_above,
            *self.approve_above,
            *self.window_caps,
        ]
        for line in lines:
            if type(line.amount) is not int or line.amount < 0:
                raise ValueError(f"{line!r}: amount must be a non-negative int of minor units")
            if line.kind not in AMOUNT_KINDS:
                # A rule over a command kind that carries no single amount would never fire
                # and would suggest a governance that does not exist.
                raise ValueError(
                    f"{line!r}: rules apply to {sorted(AMOUNT_KINDS)}; {line.kind!r} carries"
                    " no single amount"
                )
            if not isinstance(line.currency, str) or len(line.currency) != 3:
                raise ValueError(f"{line!r}: currency must be a three-letter code")
        for cap in self.window_caps:
            seconds = cap.window.total_seconds()
            if seconds <= 0 or seconds != int(seconds):
                raise ValueError(f"{cap!r}: window must be a positive whole number of seconds")

    def configuration(self) -> dict[str, Any]:
        """The declarative rules as a JSON document; :meth:`from_configuration` inverts it, so
        a consumer holding the document can recompute every decision this set made."""
        return {
            "set": set_name(type(self)),
            "version": self.version,
            "deny_above": [{**asdict(x), "amount": str(x.amount)} for x in self.deny_above],
            "approve_above": [{**asdict(x), "amount": str(x.amount)} for x in self.approve_above],
            "window_caps": [
                {**asdict(c), "amount": str(c.amount), "window": int(c.window.total_seconds())}
                for c in self.window_caps
            ],
            "gated_reads": sorted(self.gated_reads),
        }

    @classmethod
    def from_configuration(cls, doc: Mapping[str, Any]) -> ThresholdPolicySet:
        if doc.get("set") != set_name(cls):
            raise ValueError(f"configuration is for {doc.get('set')!r}, not {set_name(cls)}")
        return cls(
            version=str(doc["version"]),
            deny_above=[
                Threshold(x["kind"], x["currency"], _whole(x["amount"])) for x in doc["deny_above"]
            ],
            approve_above=[
                Threshold(x["kind"], x["currency"], _whole(x["amount"]))
                for x in doc["approve_above"]
            ],
            window_caps=[
                WindowCap(
                    c["kind"],
                    c["currency"],
                    _whole(c["amount"]),
                    timedelta(seconds=_bounded_window(_whole(c["window"]))),
                )
                for c in doc["window_caps"]
            ],
            gated_reads=frozenset(doc["gated_reads"]),
        )

    def configuration_digest(self) -> str:
        from ledgergate.codec import digest

        return digest(self.configuration())

    def gates_read(self, tool: str) -> bool:
        return tool in self.gated_reads

    def subject_of(self, command: Command) -> str | None:
        match command:
            case OpenTransaction(_, transaction_id, _) | Advance(_, transaction_id, _, _):
                return transaction_id
            case Refund(_, transaction_id, _, _):
                return transaction_id
        return None

    def aggregates_for(self, command: Command, now: datetime, history: History) -> dict[str, Any]:
        subject, kind, money = (
            self.subject_of(command),
            command_kind(command),
            command_amount(command),
        )
        if subject is None or money is None:
            return {}
        out: dict[str, Any] = {}
        for cap in self.window_caps:
            if cap.kind == kind and cap.currency == money.currency.code:
                total = history.applied_total(
                    subject=subject, kind=kind, currency=cap.currency, since=now - cap.window
                )
                out[f"applied.{kind}.{cap.currency}.{int(cap.window.total_seconds())}s"] = str(
                    total
                )
        return out

    def evaluate(self, context: PolicyContext) -> Decision:
        if context.digest_kind == "request":
            return Decision(
                "allow", f"{self.version}.read_recorded", "this set gates reads for evidence only"
            )
        if context.amount is None or context.currency is None or context.command_kind is None:
            return Decision("allow", f"{self.version}.no_amount", "command carries no amount")
        amount, kind, ccy = int(context.amount), context.command_kind, context.currency
        for line in self.deny_above:
            if line.kind == kind and line.currency == ccy and amount > line.amount:
                return Decision(
                    "deny",
                    f"{self.version}.deny_above",
                    f"{kind} of {amount} {ccy} exceeds the hard limit {line.amount}",
                )
        for cap in self.window_caps:
            if cap.kind == kind and cap.currency == ccy:
                name = f"applied.{kind}.{ccy}.{int(cap.window.total_seconds())}s"
                if name not in context.aggregates:
                    raise ValueError(
                        f"context lacks aggregate {name!r}: it was built for a different set"
                    )
                prior = int(context.aggregates[name])
                if prior + amount > cap.amount:
                    return Decision(
                        "deny",
                        f"{self.version}.window_cap",
                        f"{kind} total {prior + amount} {ccy} for the subject would exceed"
                        f" the cap {cap.amount} within {cap.window}",
                    )
        for line in self.approve_above:
            if line.kind == kind and line.currency == ccy and amount > line.amount:
                if (
                    context.approval is not None
                    and context.approval.get("verdict") == "approval_valid"
                ):
                    return Decision(
                        "allow",
                        f"{self.version}.approved",
                        f"{kind} of {amount} {ccy} above {line.amount} carries a valid approval",
                    )
                return Decision(
                    "approval_required",
                    f"{self.version}.approve_above",
                    f"{kind} of {amount} {ccy} exceeds {line.amount} and needs an approval",
                )
        return Decision("allow", f"{self.version}.within_limits", "within every configured line")
