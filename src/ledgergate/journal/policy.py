# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""The policy seam: an explicit, serializable context and a pure decision.

M2b ships the null policy set (version ``none``): it allows every context and gates no
read, and it still produces a complete decision row, so every operation has a decision
from day one and M3's real policy sets replace an implementation, not the protocol.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

DecisionKind = Literal["allow", "deny", "approval_required"]
DigestKind = Literal["fingerprint", "request"]


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """Everything a policy may look at. Persisted verbatim with every decision.

    ``subject`` is nullable: the core has no notion of a subject, and a policy set declares
    how it is derived from the command per intent kind. Under the null set it is ``None``.
    Aggregates are the historical values the rules read, so a decision replays without
    live state. Money amounts inside aggregates are decimal strings, never integers.
    """

    principal: str
    subject: str | None
    command_digest: str
    digest_kind: DigestKind
    evaluated_at: datetime
    policy_set_version: str
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


class PolicySet(Protocol):
    version: str

    def gates_read(self, tool: str) -> bool: ...

    def evaluate(self, context: PolicyContext) -> Decision: ...


class NullPolicySet:
    """Allows everything, gates nothing, and says so in every decision row."""

    version = "none"

    def gates_read(self, tool: str) -> bool:
        return False

    def evaluate(self, context: PolicyContext) -> Decision:
        return Decision("allow", "none.allow_all", "null policy set: no rules configured")
