# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""``trace(journal) -> TraceV2``: deterministic derivation of a v2 trace from a journal.

Ordering is *invocation-anchored* (``docs/spec/journal.md`` *Trace derivation*,
``docs/spec/trace-v2.md`` *Ordering*): every event derived from one invocation is placed at
``(invocation.journal_sequence, ordinal)`` whatever row its data comes from, so the
``tool_call`` precedes the ``command_intent`` even though its row was written later.
``invocation_resolution`` names the exact outcome the response row names, never the
operation's current outcome. Standalone messages sit at their own row's sequence. Reading
is a single read-only snapshot and needs no key.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from ledgergate.codec import CODEC_VERSION
from ledgergate.journal.schema import SCHEMA_VERSION
from ledgergate.trace.models import (
    AccountDoc,
    AgentDoc,
    CurrencyDoc,
    ErrorDoc,
    LedgerCommandEvent,
    LedgerResultEvent,
    MessageEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from ledgergate.trace.v2 import (
    ApprovalRef,
    CommandIntent,
    InvocationResolution,
    PolicyDecision,
    ReadIntent,
    ReadResult,
    TraceV2,
)


class DerivationError(ValueError):
    """The journal cannot be read as a trace (wrong version, missing definition)."""


def intent_id(invocation: int) -> str:
    return f"intent-{invocation}"


def command_id(operation: int) -> str:
    return f"command-{operation}"


def outcome_ref(outcome: int) -> str:
    return f"outcome-{outcome}"


def presentation_ref(presentation: int) -> str:
    return f"presentation-{presentation}"


def consumption_ref(consumption: int) -> str:
    return f"consumption-{consumption}"


def trace(path: str, *, trace_id: str | None = None) -> TraceV2:
    """Derive the whole journal at ``path`` into one v2 document."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # One read snapshot for the whole derivation. Under WAL a deferred transaction
        # takes its snapshot at the first read, so read immediately after BEGIN.
        conn.execute("BEGIN")
        conn.execute("SELECT COUNT(*) FROM journal").fetchone()
        try:
            return _Derivation(conn).run(trace_id)
        finally:
            conn.execute("COMMIT")
    finally:
        conn.close()


class _Derivation:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.c = conn

    def run(self, trace_id: str | None) -> TraceV2:
        d = self.c.execute("SELECT * FROM definition").fetchone()
        if d is None:
            raise DerivationError("journal has no definition")
        if d["schema_version"] != SCHEMA_VERSION or d["codec_version"] != CODEC_VERSION:
            raise DerivationError(
                f"journal is schema {d['schema_version']}/codec {d['codec_version']!r};"
                f" this build derives schema {SCHEMA_VERSION}/codec {CODEC_VERSION!r}"
            )
        currencies = dict(json.loads(d["currencies"]).items())
        chart = json.loads(d["chart"])
        anchored: list[tuple[tuple[int, int], Any]] = []
        last_at = datetime.fromisoformat(d["created_at"])

        for ev in self.c.execute(
            "SELECT * FROM events WHERE invocation IS NULL ORDER BY journal_sequence"
        ):
            body = json.loads(ev["body"])
            at = datetime.fromisoformat(body["at"])
            last_at = max(last_at, at)
            anchored.append(
                (
                    (ev["journal_sequence"], 0),
                    MessageEvent(seq=1, at=at, role=body["role"], content=body["content"]),
                )
            )

        for inv in self.c.execute("SELECT * FROM invocations ORDER BY journal_sequence"):
            at = datetime.fromisoformat(inv["requested_at"])
            last_at = max(last_at, at)
            anchored.extend(((inv["journal_sequence"], o), e) for o, e in self._invocation(inv, at))

        anchored.sort(key=lambda x: x[0])
        events = tuple(ev.model_copy(update={"seq": i + 1}) for i, (_k, ev) in enumerate(anchored))
        return TraceV2(
            trace_id=trace_id or f"journal-{d['journal_id']}",
            journal_id=d["journal_id"],
            agent=AgentDoc(name="ledgergate-journal"),
            started_at=datetime.fromisoformat(d["created_at"]),
            ended_at=last_at,
            currencies=tuple(
                CurrencyDoc(code=code, exponent=exp) for code, exp in sorted(currencies.items())
            ),
            chart=tuple(
                AccountDoc(
                    account_id=a["account_id"],
                    kind=a["kind"],
                    currency=a["currency"],
                    allow_negative=a["allow_negative"],
                    name=a["name"],
                )
                for a in chart
            ),
            policy_set_version=d["policy_set_version"],
            events=events,
        )

    # ------------------------------------------------------------ per invocation

    def _invocation(self, inv: sqlite3.Row, at: datetime) -> list[tuple[int, Any]]:
        seq = inv["journal_sequence"]
        iid = intent_id(seq)
        disposition = inv["disposition"]
        inbound, outbound = self._events_for(seq)
        response = self.c.execute(
            "SELECT * FROM invocation_responses WHERE invocation = ?", (seq,)
        ).fetchone()
        if response is None:
            raise DerivationError(f"invocation {seq} has no response row")
        presentations = self.c.execute(
            "SELECT journal_sequence FROM approvals WHERE invocation = ?", (seq,)
        ).fetchall()
        if len(presentations) > 1:
            raise DerivationError(f"invocation {seq} has {len(presentations)} presentations")
        presentation = presentations[0] if presentations else None
        out: list[tuple[int, Any]] = []

        # 0: tool_call
        if disposition == "invalid":
            call_id = inbound.get("call_id") or f"invalid-{seq}"
            tool = inbound.get("tool") or "unknown"
            out.append((0, ToolCallEvent(seq=1, at=at, call_id=call_id, tool=tool, arguments={})))
            digest = inbound["input_digest"]
        else:
            call_id = inbound["call_id"]
            out.append(
                (
                    0,
                    ToolCallEvent(
                        seq=1,
                        at=at,
                        call_id=call_id,
                        tool=inbound["tool"],
                        arguments=inbound["arguments"],
                        idempotency_key=self._key_of(inv),
                    ),
                )
            )
            digest = (
                inv["attempted_fingerprint"] if disposition != "read" else inv["request_digest"]
            )

        # 1: intent
        if disposition == "read":
            out.append(
                (
                    1,
                    ReadIntent(
                        seq=1,
                        at=at,
                        intent_id=iid,
                        call_id=call_id,
                        tool=inbound["tool"],
                        arguments=inbound["arguments"],
                    ),
                )
            )
        elif disposition != "invalid":
            out.append(
                (
                    1,
                    CommandIntent(
                        seq=1,
                        at=at,
                        intent_id=iid,
                        call_id=call_id,
                        command=json.loads(inv["attempted_command"]),
                    ),
                )
            )

        # 2: resolution
        out.append(
            (
                2,
                InvocationResolution(
                    seq=1,
                    at=at,
                    intent_id=iid,
                    disposition=disposition,
                    operation_id=None if inv["operation"] is None else command_id(inv["operation"]),
                    outcome_ref=None
                    if response["outcome"] is None
                    else outcome_ref(response["outcome"]),
                    attempted_digest=digest,
                    presentation_ref=None
                    if presentation is None
                    else presentation_ref(presentation[0]),
                ),
            )
        )

        # 3: decision
        decision = self.c.execute("SELECT * FROM decisions WHERE invocation = ?", (seq,)).fetchone()
        if decision is not None:
            out.append(
                (
                    3,
                    PolicyDecision(
                        seq=1,
                        at=at,
                        intent_id=iid,
                        policy_set_version=decision["policy_set_version"],
                        decision=decision["decision"],
                        matched_rule=decision["matched_rule"],
                        reason=decision["reason"],
                        context=json.loads(decision["context"]),
                        approval=None
                        if decision["presentation"] is None
                        else ApprovalRef(
                            presentation_ref=presentation_ref(decision["presentation"]),
                            verdict=decision["approval_verdict"],
                        ),
                        consumption_ref=None
                        if decision["consumption"] is None
                        else consumption_ref(decision["consumption"]),
                    ),
                )
            )

        # 4-5: ledger pair, iff this invocation produced an applied/rejected outcome
        if (
            decision is not None
            and decision["decision"] == "allow"
            and disposition in ("new", "approval")
        ):
            outcome = self.c.execute(
                "SELECT * FROM outcomes WHERE journal_sequence = ?", (response["outcome"],)
            ).fetchone()
            cid = command_id(inv["operation"])
            out.append(
                (
                    4,
                    LedgerCommandEvent(
                        seq=1,
                        at=at,
                        command_id=cid,
                        call_id=call_id,
                        command=json.loads(inv["attempted_command"]),
                    ),
                )
            )
            out.append((5, self._ledger_result(outcome, cid, at)))

        # 6: read_result
        if disposition == "read":
            rd = self.c.execute("SELECT * FROM reads WHERE invocation = ?", (seq,)).fetchone()
            if rd is not None:
                out.append(
                    (
                        6,
                        ReadResult(
                            seq=1,
                            at=at,
                            intent_id=iid,
                            cursor=rd["cursor"],
                            head=rd["head"],
                            result_digest=rd["result_digest"],
                        ),
                    )
                )

        # 7: tool_result
        out.append(
            (
                7,
                ToolResultEvent(
                    seq=1,
                    at=at,
                    call_id=call_id,
                    ok=outbound["ok"],
                    result=outbound.get("result"),
                    error=None if outbound["ok"] else ErrorDoc(**outbound["error"]),
                ),
            )
        )
        return out

    def _ledger_result(self, outcome: sqlite3.Row, cid: str, at: datetime) -> LedgerResultEvent:
        if outcome["outcome"] == "applied":
            posted = outcome["posted_at"]
            return LedgerResultEvent(
                seq=1,
                at=at,
                command_id=cid,
                ok=True,
                replayed=False,
                head=outcome["head_after"],
                sequence=outcome["ledger_sequence"],
                entry_id=outcome["entry_id"],
                posted_at=None if posted is None else datetime.fromisoformat(posted),
            )
        return LedgerResultEvent(
            seq=1,
            at=at,
            command_id=cid,
            ok=False,
            error=ErrorDoc(type=outcome["error_type"], message=outcome["error_message"]),
            head=outcome["head_after"],
            sequence=outcome["ledger_sequence"],
        )

    def _events_for(self, inv_seq: int) -> tuple[dict[str, Any], dict[str, Any]]:
        rows = self.c.execute(
            "SELECT direction, body FROM events WHERE invocation = ?", (inv_seq,)
        ).fetchall()
        if [r["direction"] for r in rows].count("inbound") != 1 or [
            r["direction"] for r in rows
        ].count("outbound") != 1:
            raise DerivationError(f"invocation {inv_seq} lacks exactly one event per direction")
        by = {r["direction"]: json.loads(r["body"]) for r in rows}
        return by["inbound"], by["outbound"]

    def _key_of(self, inv: sqlite3.Row) -> str | None:
        if inv["operation"] is None:
            return None
        row = self.c.execute(
            "SELECT key FROM operations WHERE journal_sequence = ?", (inv["operation"],)
        ).fetchone()
        return None if row is None else str(row["key"])


__all__ = [
    "DerivationError",
    "command_id",
    "consumption_ref",
    "intent_id",
    "outcome_ref",
    "presentation_ref",
    "trace",
]
