"""Properties of the journal under arbitrary command sequences.

Whatever a caller does, in whatever order, with whatever retries: the journal reopens to the
projection it had, applies each key at most once, records every attempt, and its rows
satisfy the constraints the specification names.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.unit.journal.support import CHART, balance, open_txn, post, sale_doc

from ledgergate.journal import FACT_TABLES, Journal
from ledgergate.ledger import EPOCH, SequentialIds, SteppingClock


def _write(key: str, i: int, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"tool": tool, "call_id": f"c{i}", "key": key, "arguments": arguments}


def refund_doc(amount: int) -> dict[str, Any]:
    return {
        "postings": [
            {"account": "revenue", "side": "debit", "money": {"amount": amount, "currency": "USD"}},
            {"account": "cash", "side": "credit", "money": {"amount": amount, "currency": "USD"}},
        ]
    }


@st.composite
def requests(draw: st.DrawFn) -> list[dict[str, Any]]:
    keys = [f"k{i}" for i in range(4)]
    out: list[dict[str, Any]] = []
    for i in range(draw(st.integers(1, 12))):
        key = draw(st.sampled_from(keys))
        kind = draw(
            st.sampled_from(
                [
                    "post",
                    "post",
                    "open",
                    "authorize",
                    "settle",
                    "refund",
                    "reverse",
                    "read",
                    "invalid",
                ]
            )
        )
        txn = f"t-{draw(st.sampled_from(keys))}"
        if kind == "post":
            out.append(post(key, call_id=f"c{i}", amount=draw(st.sampled_from([5, 5, 7]))))
        elif kind == "open":
            out.append(open_txn(key, txn, amount=100))
        elif kind == "authorize":
            out.append(_write(key, i, "advance", {"transaction_id": txn, "event": "authorize"}))
        elif kind == "settle":
            out.append(
                _write(
                    key,
                    i,
                    "advance",
                    {"transaction_id": txn, "event": "settle", "entry": sale_doc(100)},
                )
            )
        elif kind == "refund":
            out.append(
                _write(
                    key,
                    i,
                    "refund",
                    {
                        "transaction_id": txn,
                        "money": {"amount": 40, "currency": "USD"},
                        "entry": refund_doc(40),
                    },
                )
            )
        elif kind == "reverse":
            out.append(_write(key, i, "reverse", {"entry_id": f"e-{draw(st.integers(1, 6)):06d}"}))
        elif kind == "read":
            out.append(balance("cash", call_id=f"c{i}"))
        else:
            out.append({"tool": "post", "call_id": f"c{i}", "key": key, "arguments": {}})
    return out


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(requests())
def test_journal_invariants_hold_for_any_sequence(
    tmp_path: Path, reqs: list[dict[str, Any]]
) -> None:
    import uuid

    path = str(tmp_path / f"{uuid.uuid4().hex}.journal")  # hypothesis reuses tmp_path per test
    j = Journal.create(path, CHART, clock=SteppingClock(EPOCH), ids=SequentialIds())
    responses = [j.handle(r) for r in reqs]
    head, cursor, seq = j.ledger.head, j.cursor, j.ledger.sequence
    j.close()

    conn = sqlite3.connect(path)
    try:
        # every attempt is recorded, once
        assert conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0] == len(reqs)
        assert conn.execute("SELECT COUNT(*) FROM invocation_responses").fetchone()[0] == len(reqs)
        # each key applied at most once: one operation per distinct key that was admitted
        keys_admitted = {
            r["key"]
            for r, resp in zip(reqs, responses, strict=True)
            if "key" in r and resp.disposition != "invalid"
        }
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == len(keys_admitted)
        # every operation has exactly one outcome (no approvals under the null policy)
        without_one_outcome = conn.execute(
            "SELECT COUNT(*) FROM operations op WHERE"
            " (SELECT COUNT(*) FROM outcomes o WHERE o.operation = op.journal_sequence) <> 1"
        ).fetchone()[0]
        assert without_one_outcome == 0
        # every fact row's allocator row has the right kind
        for table in FACT_TABLES:
            bad = conn.execute(
                f"SELECT COUNT(*) FROM {table} t"
                " JOIN journal j ON j.journal_sequence = t.journal_sequence WHERE j.kind <> ?",
                (table,),
            ).fetchone()[0]
            assert bad == 0
        # the cursor is the latest outcome
        latest = conn.execute("SELECT COALESCE(MAX(journal_sequence), 0) FROM outcomes").fetchone()[
            0
        ]
        assert cursor == latest
    finally:
        conn.close()

    again = Journal.open(path, clock=SteppingClock(EPOCH), ids=SequentialIds(start=999))
    try:
        assert (again.ledger.head, again.cursor, again.ledger.sequence) == (head, cursor, seq)
        # a replay after reopen answers exactly what the first application did
        for r, resp in zip(reqs, responses, strict=True):
            if resp.response == "applied":
                r2 = again.handle({**r, "call_id": r["call_id"] + "-again"})
                assert r2.response == "replayed" and r2.result.get("entry_id") == resp.result.get(
                    "entry_id"
                )
                break
    finally:
        again.close()
