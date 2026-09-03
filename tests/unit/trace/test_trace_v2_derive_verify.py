"""Trace v2, journal derivation, the invariant registry and `ledgergate verify`, held to
docs/spec/trace-v2.md and docs/spec/journal.md (*Trace derivation*)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ledgergate.cli.__main__ import main
from ledgergate.derive import DerivationError
from ledgergate.derive import trace as derive
from ledgergate.invariants import REGISTRY, check
from ledgergate.journal import (
    Journal,
    Threshold,
    ThresholdPolicySet,
    generate_signing_key,
    issue,
    verification_key_text,
)
from ledgergate.ledger import (
    EPOCH,
    USD,
    Account,
    AccountType,
    ChartOfAccounts,
    LedgerError,
    Money,
    OpenTransaction,
    Refund,
    SequentialIds,
    SteppingClock,
)
from ledgergate.trace import (
    AgentDoc,
    InvocationResolution,
    Recorder,
    TraceV2,
    dump_trace,
    dump_v2,
    lift,
    load_any,
    replay_trace,
)

CHART = ChartOfAccounts(
    [Account("cash", AccountType.ASSET, USD), Account("revenue", AccountType.REVENUE, USD)]
)
SIGNER = generate_signing_key()
POLICY = ThresholdPolicySet(
    version="p",
    approve_above=[Threshold("open_transaction", "USD", 100)],
    gated_reads=frozenset({"balance"}),
)
SALE = {
    "postings": [
        {"account": "cash", "side": "debit", "money": {"amount": 5, "currency": "USD"}},
        {"account": "revenue", "side": "credit", "money": {"amount": 5, "currency": "USD"}},
    ]
}


def _open(key: str, call_id: str, amount: int, **extra: Any) -> dict[str, Any]:
    return {
        "tool": "open_transaction",
        "call_id": call_id,
        "key": key,
        "arguments": {"transaction_id": "t", "amount": {"amount": amount, "currency": "USD"}},
        **extra,
    }


@pytest.fixture
def journal_path(tmp_path: Path) -> Iterator[str]:
    """A journal exercising every disposition: new (applied, rejected, awaiting), replay,
    conflict, invalid, gated read, approval, and a standalone message."""
    path = str(tmp_path / "j.journal")
    j = Journal.create(
        path,
        CHART,
        clock=SteppingClock(EPOCH),
        ids=SequentialIds(),
        policy=POLICY,
        approval_key=verification_key_text(SIGNER),
    )
    j.record_message("user", "go")
    j.handle({"tool": "post", "call_id": "c1", "key": "k1", "arguments": {"draft": SALE}})
    j.handle({"tool": "post", "call_id": "c2", "key": "k1", "arguments": {"draft": SALE}})
    j.handle(
        {
            "tool": "post",
            "call_id": "c3",
            "key": "k1",
            "arguments": {"draft": {**SALE, "description": "x"}},
        }
    )
    j.handle(_open("big", "c4", 500))
    j.handle("junk")
    j.handle({"tool": "balance", "call_id": "c5", "arguments": {"account": "cash"}})
    j.handle(
        {
            "tool": "refund",
            "call_id": "c6",
            "key": "r",
            "arguments": {"transaction_id": "ghost", "money": {"amount": 1, "currency": "USD"}},
        }
    )
    import sqlite3

    conn = sqlite3.connect(path)
    (fp,) = conn.execute("SELECT fingerprint FROM operations WHERE key = 'big'").fetchone()
    conn.close()
    art = issue(
        SIGNER,
        journal_id=j.definition.journal_id,
        approval_id="a1",
        approver="cfo",
        fingerprint=fp,
        key="big",
        issued_at=EPOCH,
        expires_at=EPOCH + timedelta(days=1),
    ).to_json()
    j.handle(_open("big", "c7", 500, approval=art))
    j.close()
    yield path


class TestDerivation:
    def test_every_disposition_derives_with_the_spec_grammar(self, journal_path: str) -> None:
        t = derive(journal_path)
        kinds = [e.type for e in t.events]
        assert kinds[:2] == ["message", "tool_call"]
        by = {r.disposition: r for r in t.resolutions()}
        assert set(by) == {"new", "replay", "conflict", "invalid", "read", "approval"}
        # anchored order: tool_call, intent, resolution, [decision], [pair], tool_result
        new_id = by["new"].intent_id
        first = [e for e in t.events if getattr(e, "intent_id", None) == new_id]
        assert [e.type for e in first] == [
            "command_intent",
            "invocation_resolution",
            "policy_decision",
        ]
        i = t.events.index(first[0])
        assert t.events[i - 1].type == "tool_call"  # anchored before the intent
        assert [e.type for e in t.events[i + 3 : i + 6]] == [
            "ledger_command",
            "ledger_result",
            "tool_result",
        ]
        replay = by["replay"]
        producer = next(
            r
            for r in t.resolutions()
            if r.disposition == "new" and r.operation_id == replay.operation_id
        )
        assert replay.outcome_ref == producer.outcome_ref  # the exact outcome that answered
        assert by["conflict"].outcome_ref is None and by["invalid"].operation_id is None
        assert by["approval"].presentation_ref is not None
        decisions = t.decisions()
        assert decisions[by["approval"].intent_id].approval is not None
        assert decisions[by["approval"].intent_id].approval.verdict == "approval_valid"  # type: ignore[union-attr]
        assert decisions[by["approval"].intent_id].consumption_ref is not None
        assert decisions[by["read"].intent_id].context["digest_kind"] == "request"
        assert t.journal_id is not None and t.policy_set_version == "p"

    def test_derivation_is_deterministic_and_round_trips(self, journal_path: str) -> None:
        a, b = derive(journal_path), derive(journal_path)
        assert a == b
        assert TraceV2.model_validate_json(dump_v2(a)) == a
        assert load_any(dump_v2(a)) == a

    def test_derived_ledger_pairs_replay(self, journal_path: str) -> None:
        report = replay_trace(derive(journal_path).ledger_view())
        assert report.consistent and report.commands_replayed == 3  # applied, rejected, approved

    def test_a_replay_names_the_outcome_that_answered_then_not_now(self, tmp_path: Path) -> None:
        path = str(tmp_path / "r.journal")
        j = Journal.create(
            path,
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=POLICY,
            approval_key=verification_key_text(SIGNER),
        )
        j.handle(_open("big", "c1", 500))  # awaiting
        j.handle(_open("big", "c2", 500))  # replay of awaiting
        import sqlite3

        conn = sqlite3.connect(path)
        (fp,) = conn.execute("SELECT fingerprint FROM operations").fetchone()
        conn.close()
        art = issue(
            SIGNER,
            journal_id=j.definition.journal_id,
            approval_id="a",
            approver="cfo",
            fingerprint=fp,
            key="big",
            issued_at=EPOCH,
            expires_at=EPOCH + timedelta(days=1),
        ).to_json()
        j.handle(_open("big", "c3", 500, approval=art))  # applied
        j.close()
        t = derive(path)
        new, replay, approval = t.resolutions()
        assert new.outcome_ref == replay.outcome_ref  # the awaiting outcome, then
        assert approval.outcome_ref != replay.outcome_ref  # the applied one, later
        tool_results = [e for e in t.events if e.type == "tool_result"]
        assert not tool_results[1].ok and tool_results[1].error.type == "ApprovalRequired"  # type: ignore[union-attr]

    def test_other_schema_version_is_refused(self, journal_path: str) -> None:
        import sqlite3

        conn = sqlite3.connect(journal_path, isolation_level=None)
        conn.execute("DROP TRIGGER definition_no_update")
        conn.execute("UPDATE definition SET codec_version = '9'")
        conn.close()
        with pytest.raises(DerivationError, match="codec"):
            derive(journal_path)


class TestGrammar:
    def _base(self, events: list[Any]) -> dict[str, Any]:
        return {
            "trace_id": "t",
            "started_at": EPOCH.isoformat(),
            "ended_at": EPOCH.isoformat(),
            "policy_set_version": "none",
            "events": events,
        }

    def _res(self, seq: int, iid: str, disposition: str, **kw: Any) -> dict[str, Any]:
        return {
            "type": "invocation_resolution",
            "seq": seq,
            "at": EPOCH.isoformat(),
            "intent_id": iid,
            "disposition": disposition,
            "attempted_digest": "0" * 64,
            **kw,
        }

    def test_invalid_carries_nothing_else(self) -> None:
        TraceV2.model_validate(self._base([self._res(1, "i", "invalid")]))
        with pytest.raises(ValidationError, match="invalid carries no intent"):
            TraceV2.model_validate(
                self._base(
                    [
                        {
                            "type": "read_intent",
                            "seq": 1,
                            "at": EPOCH.isoformat(),
                            "intent_id": "i",
                            "call_id": "c",
                            "tool": "balance",
                            "arguments": {},
                        },
                        self._res(2, "i", "invalid"),
                    ]
                )
            )

    def test_new_requires_a_decision_and_replay_forbids_one(self) -> None:
        cmd = {"kind": "reverse", "key": "k", "entry_id": "e"}
        intent = {
            "type": "command_intent",
            "seq": 1,
            "at": EPOCH.isoformat(),
            "intent_id": "i",
            "call_id": "c",
            "command": cmd,
        }
        with pytest.raises(ValidationError, match="exactly one policy_decision"):
            TraceV2.model_validate(
                self._base([intent, self._res(2, "i", "new", operation_id="op", outcome_ref="o")])
            )
        decision = {
            "type": "policy_decision",
            "seq": 3,
            "at": EPOCH.isoformat(),
            "intent_id": "i",
            "policy_set_version": "none",
            "decision": "deny",
            "matched_rule": "r",
            "reason": "x",
            "context": {},
        }
        with pytest.raises(ValidationError, match="never carries a policy_decision"):
            TraceV2.model_validate(
                self._base(
                    [
                        intent,
                        self._res(2, "i", "replay", operation_id="op", outcome_ref="o"),
                        decision,
                    ]
                )
            )
        TraceV2.model_validate(
            self._base(
                [intent, self._res(2, "i", "new", operation_id="op", outcome_ref="o"), decision]
            )
        )

    def test_resolution_shape(self) -> None:
        with pytest.raises(ValidationError, match="carries no operation"):
            InvocationResolution(
                seq=1,
                at=EPOCH,
                intent_id="i",
                disposition="read",
                operation_id="op",
                attempted_digest="0" * 64,
            )
        with pytest.raises(ValidationError, match="outcome_ref is present exactly"):
            InvocationResolution(
                seq=1,
                at=EPOCH,
                intent_id="i",
                disposition="conflict",
                operation_id="op",
                outcome_ref="o",
                attempted_digest="0" * 64,
            )

    def test_each_intent_has_exactly_one_resolution(self) -> None:
        with pytest.raises(ValidationError, match="exactly one invocation_resolution"):
            TraceV2.model_validate(
                self._base([self._res(1, "i", "invalid"), self._res(2, "i", "invalid")])
            )


class TestLift:
    def _v1(self) -> Recorder:
        rec = Recorder("t", AgentDoc(name="a"), CHART, SteppingClock(EPOCH), SequentialIds())
        rec.message("user", "hi")
        rec.tool_call("c1", "open", {"x": 1}, idempotency_key="o")
        rec.execute(OpenTransaction("o", "t", Money(1, USD)), call_id="c1")
        rec.tool_result("c1", True, {"ok": 1})
        with pytest.raises(LedgerError):
            rec.execute(Refund("r", "ghost", Money(1, USD)))
        return rec

    def test_lift_invents_nothing_and_keeps_order(self) -> None:
        v2 = lift(self._v1().trace())
        kinds = [e.type for e in v2.events]
        assert kinds == [
            "message",
            "tool_call",
            "legacy_intent",
            "invocation_resolution",
            "ledger_command",
            "ledger_result",
            "tool_result",
            "legacy_intent",
            "invocation_resolution",
            "ledger_command",
            "ledger_result",
        ]
        assert all(r.disposition == "legacy" for r in v2.resolutions())
        assert not v2.decisions() and v2.policy_set_version == "legacy"
        assert [e.seq for e in v2.events] == list(range(1, 12))
        assert replay_trace(v2.ledger_view()).consistent

    def test_load_any_lifts_v1_and_loads_v2(self) -> None:
        v1_text = dump_trace(self._v1().trace())
        lifted = load_any(v1_text)
        assert lifted.schema_version == "2" and load_any(dump_v2(lifted)) == lifted


class TestInvariants:
    def test_a_good_journal_passes_and_legacy_reports_no_evidence(self, journal_path: str) -> None:
        card = check(derive(journal_path))
        assert card.passed and {r.name for r in card.results} == {i.name for i in REGISTRY}
        assert {r.status for r in card.results} == {"pass", "no_evidence"}  # legacy: no evidence
        assert (
            next(r for r in card.results if r.name == "legacy_carries_no_policy_evidence").status
            == "no_evidence"
        )

    def test_a_lifted_v1_trace_reports_no_evidence_for_policy_invariants(self) -> None:
        rec = Recorder("t", AgentDoc(name="a"), CHART, SteppingClock(EPOCH), SequentialIds())
        rec.execute(OpenTransaction("o", "t", Money(1, USD)))
        card = check(lift(rec.trace()))
        statuses = {r.name: r.status for r in card.results}
        assert statuses["denied_never_reaches_ledger"] == "no_evidence"
        assert (
            statuses["ledger_pairs_replay"] == "pass"
            and statuses["legacy_carries_no_policy_evidence"] == "pass"
        )
        assert card.passed

    def test_tampered_evidence_fails_the_right_invariant(self, journal_path: str) -> None:
        t = derive(journal_path)
        doc = t.model_dump(mode="json")
        # flip a recorded head: the ledger pair no longer replays
        for e in doc["events"]:
            if e["type"] == "ledger_result" and e["ok"]:
                e["head"] = "f" * 64
                break
        bad = TraceV2.model_validate(doc)
        card = check(bad)
        assert not card.passed
        assert next(r for r in card.results if r.name == "ledger_pairs_replay").status == "fail"
        # a runtime-written decision that is not a failed verdict
        doc2 = t.model_dump(mode="json")
        d = next(
            e for e in doc2["events"] if e["type"] == "policy_decision" and e["decision"] == "allow"
        )
        d["matched_rule"] = "runtime.invented"
        card2 = check(TraceV2.model_validate(doc2))
        assert (
            next(r for r in card2.results if r.name == "runtime_decisions_are_verdicts").status
            == "fail"
        )
        # a context that disagrees with its decision about the verdict
        doc3 = t.model_dump(mode="json")
        d3 = next(e for e in doc3["events"] if e["type"] == "policy_decision" and e.get("approval"))
        d3["context"]["approval"] = {"presentation": 1, "verdict": "approval_expired"}
        card3 = check(TraceV2.model_validate(doc3))
        assert (
            next(r for r in card3.results if r.name == "context_matches_decision").status == "fail"
        )

    def test_registry_entries_are_grounded(self) -> None:
        for inv in REGISTRY:
            assert inv.description and inv.source and inv.name == inv.check.__name__


class TestVerifyCli:
    def test_verify_journal_and_traces(
        self, journal_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "derived.json"
        assert main(["verify", journal_path, "--emit-trace", str(out)]) == 0
        text = capsys.readouterr().out
        assert text.startswith("pass") or "pass" in text
        assert "PASS:" in text
        assert main(["verify", str(out), "--json"]) == 0
        card = json.loads(capsys.readouterr().out)
        assert card["passed"] is True and card["intents"] == 8

    def test_verify_fails_on_tampered_trace_and_exits_2_on_garbage(
        self, journal_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        t = derive(journal_path)
        doc = t.model_dump(mode="json")
        for e in doc["events"]:
            if e["type"] == "ledger_result" and e["ok"]:
                e["head"] = "f" * 64
                break
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(doc))
        assert main(["verify", str(bad)]) == 1
        assert "FAIL" in capsys.readouterr().out
        garbage = tmp_path / "g.json"
        garbage.write_text("{not json")
        assert main(["verify", str(garbage)]) == 2
        assert main(["verify", str(tmp_path / "missing")]) == 2

    def test_v1_trace_verifies_with_no_evidence_for_policy(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rec = Recorder("t", AgentDoc(name="a"), CHART, SteppingClock(EPOCH), SequentialIds())
        rec.execute(OpenTransaction("o", "t", Money(1, USD)))
        p = tmp_path / "v1.json"
        p.write_text(dump_trace(rec.trace()))
        assert main(["verify", str(p), "--json"]) == 0
        card = json.loads(capsys.readouterr().out)
        assert {i["status"] for i in card["invariants"]} == {"pass", "no_evidence"}


def test_schema_artefact_matches_the_models() -> None:
    """schema/trace/v2.json is generated from the models and must not drift."""
    checked_in = json.loads(Path("schema/trace/v2.json").read_text())
    generated = TraceV2.model_json_schema()
    for key in ("$schema", "$id"):
        checked_in.pop(key, None)
    assert checked_in == generated


def test_ledger_command_owner_grammar_rejects_orphan_pairs() -> None:
    cmd = {"kind": "reverse", "key": "k", "entry_id": "e"}
    with pytest.raises(ValidationError, match="precedes any intent"):
        TraceV2.model_validate(
            {
                "trace_id": "t",
                "started_at": EPOCH.isoformat(),
                "ended_at": EPOCH.isoformat(),
                "policy_set_version": "none",
                "events": [
                    {
                        "type": "ledger_command",
                        "seq": 1,
                        "at": EPOCH.isoformat(),
                        "command_id": "c",
                        "command": cmd,
                    },
                    {
                        "type": "ledger_result",
                        "seq": 2,
                        "at": EPOCH.isoformat(),
                        "command_id": "c",
                        "ok": False,
                        "error": {"type": "X", "message": "m"},
                        "head": "0" * 64,
                        "sequence": 0,
                    },
                ],
            }
        )
