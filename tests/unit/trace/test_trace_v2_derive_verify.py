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
    AT = EPOCH.isoformat()

    def _base(self, events: list[Any]) -> dict[str, Any]:
        return {
            "trace_id": "t",
            "journal_id": "0" * 32,
            "started_at": self.AT,
            "ended_at": self.AT,
            "policy_set_version": "none",
            "events": events,
        }

    def _res(self, iid: str, disposition: str, **kw: Any) -> dict[str, Any]:
        return {
            "type": "invocation_resolution",
            "seq": 1,
            "at": self.AT,
            "intent_id": iid,
            "disposition": disposition,
            **{"attempted_digest": "0" * 64, **kw},
        }

    def _bracketed(self, *inner: dict[str, Any], call_id: str = "c") -> list[dict[str, Any]]:
        call = {
            "type": "tool_call",
            "seq": 1,
            "at": self.AT,
            "call_id": call_id,
            "tool": "x",
            "arguments": {},
        }
        result = {
            "type": "tool_result",
            "seq": 1,
            "at": self.AT,
            "call_id": call_id,
            "ok": False,
            "error": {"type": "X", "message": "m"},
        }
        events = [call, *inner, result]
        return [{**e, "seq": i + 1} for i, e in enumerate(events)]

    def _validate(self, *inner: dict[str, Any]) -> TraceV2:
        return TraceV2.model_validate(self._base(self._bracketed(*inner)))

    def test_invalid_carries_nothing_else(self) -> None:
        self._validate(self._res("intent-5", "invalid"))
        read = {
            "type": "read_intent",
            "seq": 1,
            "at": self.AT,
            "intent_id": "intent-5",
            "call_id": "c",
            "tool": "balance",
            "arguments": {},
        }
        with pytest.raises(ValidationError, match="invalid carries no intent"):
            self._validate(read, self._res("intent-5", "invalid"))

    def test_new_requires_a_decision_and_replay_forbids_one(self) -> None:
        from ledgergate.codec import decode_command
        from ledgergate.ledger import CURRENCIES, command_fingerprint

        cmd = {"kind": "reverse", "key": "k", "entry_id": "e"}
        fp = command_fingerprint(decode_command(cmd, CURRENCIES))
        intent = {
            "type": "command_intent",
            "seq": 1,
            "at": self.AT,
            "intent_id": "intent-5",
            "call_id": "c",
            "command": cmd,
        }
        decision = {
            "type": "policy_decision",
            "seq": 1,
            "at": self.AT,
            "intent_id": "intent-5",
            "policy_set_version": "none",
            "decision": "deny",
            "matched_rule": "r",
            "reason": "x",
            "context": {},
        }
        with pytest.raises(ValidationError, match="exactly one policy_decision"):
            self._validate(
                intent,
                self._res(
                    "intent-5",
                    "new",
                    operation_id="command-4",
                    outcome_ref="outcome-6",
                    attempted_digest=fp,
                ),
            )
        with pytest.raises(ValidationError, match="never carries a policy_decision"):
            self._validate(
                intent,
                self._res(
                    "intent-5",
                    "replay",
                    operation_id="command-4",
                    outcome_ref="outcome-6",
                    attempted_digest=fp,
                ),
                decision,
            )
        self._validate(
            intent,
            self._res(
                "intent-5",
                "new",
                operation_id="command-4",
                outcome_ref="outcome-6",
                attempted_digest=fp,
            ),
            decision,
        )

    def test_resolution_shape(self) -> None:
        with pytest.raises(ValidationError, match="carries no operation"):
            InvocationResolution(
                seq=1,
                at=EPOCH,
                intent_id="intent-5",
                disposition="read",
                operation_id="command-4",
                attempted_digest="0" * 64,
            )
        with pytest.raises(ValidationError, match="outcome_ref is present exactly"):
            InvocationResolution(
                seq=1,
                at=EPOCH,
                intent_id="intent-5",
                disposition="conflict",
                operation_id="command-4",
                outcome_ref="outcome-6",
                attempted_digest="0" * 64,
            )

    def test_each_intent_has_exactly_one_resolution(self) -> None:
        with pytest.raises(ValidationError, match="exactly one invocation_resolution"):
            self._validate(self._res("intent-5", "invalid"), self._res("intent-5", "invalid"))


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
        assert card["passed"] is True and card["intents"] == 7  # the invalid call is no intent

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
                "journal_id": "0" * 32,
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


def _artefact(j: Journal, key: str, **over: Any) -> dict[str, Any]:
    import sqlite3

    conn = sqlite3.connect(j.path)
    (fp,) = conn.execute("SELECT fingerprint FROM operations WHERE key = ?", (key,)).fetchone()
    conn.close()
    fields: dict[str, Any] = {
        "journal_id": j.definition.journal_id,
        "approval_id": f"a-{key}",
        "approver": "cfo",
        "fingerprint": fp,
        "key": key,
        "issued_at": EPOCH,
        "expires_at": EPOCH + timedelta(days=1),
    }
    fields.update(over)
    return issue(SIGNER, **fields).to_json()


class TestEveryDispositionTheSpecSinglesOut:
    """Failed-verdict approval, denied new, denied gated read, conflict and replay with an
    artefact, awaiting then approved, and a message recorded after invocations."""

    def _journal(self, tmp_path: Path) -> str:
        from ledgergate.journal.policy import Decision, PolicyContext

        class DenyReads(ThresholdPolicySet):
            def evaluate(self, context: PolicyContext) -> Decision:
                if context.digest_kind == "request":
                    return Decision("deny", f"{self.version}.no_reads", "reads are refused")
                return super().evaluate(context)

        policy = DenyReads(
            version="d",
            deny_above=[Threshold("open_transaction", "USD", 1_000)],
            approve_above=[Threshold("open_transaction", "USD", 100)],
            gated_reads=frozenset({"balance"}),
        )
        path = str(tmp_path / "all.journal")
        j = Journal.create(
            path,
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=policy,
            approval_key=verification_key_text(SIGNER),
        )
        j.handle(_open("huge", "c1", 5_000))  # denied new
        j.handle(_open("big", "c2", 500))  # awaiting
        j.handle(_open("big", "c3", 500))  # replay of awaiting (no artefact)
        expired = _artefact(j, "big", expires_at=EPOCH + timedelta(seconds=1))
        j.handle(_open("big", "c4", 500, approval=expired))  # approval, failed verdict
        j.handle(
            _open("big", "c5", 501, approval=_artefact(j, "big", approval_id="x"))
        )  # conflict + artefact
        j.handle(
            {"tool": "balance", "call_id": "c6", "arguments": {"account": "cash"}}
        )  # denied read
        j.handle(
            _open("big", "c7", 500, approval=_artefact(j, "big", approval_id="good"))
        )  # applied
        j.handle(
            _open("big", "c8", 500, approval=_artefact(j, "big", approval_id="late"))
        )  # replay + artefact
        j.record_message("assistant", "done")
        j.close()
        return path

    def test_grammar_holds_and_names_the_right_outcomes(self, tmp_path: Path) -> None:
        t = derive(self._journal(tmp_path))
        res = t.resolutions()
        dispositions = [r.disposition for r in res]
        assert dispositions == [
            "new",
            "new",
            "replay",
            "approval",
            "conflict",
            "read",
            "approval",
            "replay",
        ]
        decisions = t.decisions()
        denied_new, awaiting, _replay1, failed, conflict, read, approved, replay2 = res
        assert (
            decisions[denied_new.intent_id].decision == "deny"
            and denied_new.outcome_ref is not None
        )
        assert failed.outcome_ref == awaiting.outcome_ref  # the pending tip, produced earlier
        d = decisions[failed.intent_id]
        assert d.runtime_written and d.reason == "approval_expired" and d.approval is not None
        assert d.approval.verdict == "approval_expired" and d.context["subject"] is None
        assert conflict.presentation_ref is not None and conflict.intent_id not in decisions
        assert read.intent_id in decisions and decisions[read.intent_id].decision == "deny"
        assert not any(e.type == "read_result" for e in t.events)
        assert approved.outcome_ref != awaiting.outcome_ref
        assert replay2.outcome_ref == approved.outcome_ref and replay2.presentation_ref is not None
        # the message recorded last carries its own time and sits last
        assert t.events[-1].type == "message" and t.events[-1].at > t.events[-2].at
        assert replay_trace(t.ledger_view()).consistent
        card = check(t)
        assert card.passed
        assert {r.name: r.status for r in card.results}["runtime_decisions_are_verdicts"] == "pass"

    def test_no_evidence_is_honest_for_a_reads_only_trace(self, tmp_path: Path) -> None:
        path = str(tmp_path / "reads.journal")
        j = Journal.create(path, CHART, clock=SteppingClock(EPOCH), ids=SequentialIds())
        j.handle({"tool": "balance", "call_id": "c", "arguments": {"account": "cash"}})
        j.close()
        statuses = {r.name: r.status for r in check(derive(path)).results}
        for name in (
            "denied_never_reaches_ledger",
            "replay_never_reevaluates",
            "every_write_was_decided",
            "runtime_decisions_are_verdicts",
            "context_matches_decision",
            "ledger_pairs_replay",
            "books_balance_and_chain_verifies",
            "legacy_carries_no_policy_evidence",
        ):
            assert statuses[name] == "no_evidence", name

    def test_a_policy_asking_approval_for_a_read_is_a_configuration_fault(
        self, tmp_path: Path
    ) -> None:
        from ledgergate.journal import ConfigurationError
        from ledgergate.journal.policy import Decision, PolicyContext

        class Odd(ThresholdPolicySet):
            def evaluate(self, context: PolicyContext) -> Decision:
                return Decision("approval_required", "odd", "asks a read to wait")

        j = Journal.create(
            str(tmp_path / "odd.journal"),
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=Odd(version="odd", gated_reads=frozenset({"balance"})),
        )
        with pytest.raises(ConfigurationError, match="read cannot await"):
            j.handle({"tool": "balance", "call_id": "c", "arguments": {"account": "cash"}})
        j.close()


class TestBoundaryGrammar:
    def test_a_runtime_intent_without_its_tool_call_is_refused(self) -> None:
        at = EPOCH.isoformat()
        base = {
            "trace_id": "t",
            "journal_id": "0" * 32,
            "started_at": at,
            "ended_at": at,
            "policy_set_version": "none",
        }
        res = {
            "type": "invocation_resolution",
            "seq": 2,
            "at": at,
            "intent_id": "intent-5",
            "disposition": "invalid",
            "attempted_digest": "0" * 64,
        }
        call = {
            "type": "tool_call",
            "seq": 1,
            "at": at,
            "call_id": "c",
            "tool": "x",
            "arguments": {},
        }
        result = {
            "type": "tool_result",
            "seq": 3,
            "at": at,
            "call_id": "c",
            "ok": False,
            "error": {"type": "X", "message": "m"},
        }
        TraceV2.model_validate({**base, "events": [call, res, result]})
        with pytest.raises(ValidationError, match="no tool_call immediately before"):
            TraceV2.model_validate({**base, "events": [{**res, "seq": 1}, {**result, "seq": 2}]})
        with pytest.raises(ValidationError, match="no matching tool_result"):
            TraceV2.model_validate({**base, "events": [call, res, {**result, "call_id": "other"}]})
        cmd = {"kind": "reverse", "key": "k", "entry_id": "e"}
        intent = {
            "type": "command_intent",
            "seq": 2,
            "at": at,
            "intent_id": "intent-5",
            "call_id": "c",
            "command": cmd,
        }
        new = {
            **res,
            "seq": 3,
            "disposition": "new",
            "operation_id": "command-4",
            "outcome_ref": "outcome-6",
        }
        decision = {
            "type": "policy_decision",
            "seq": 4,
            "at": at,
            "intent_id": "intent-5",
            "policy_set_version": "none",
            "decision": "deny",
            "matched_rule": "r",
            "reason": "x",
            "context": {},
        }
        with pytest.raises(ValidationError, match="differs from its tool_call"):
            TraceV2.model_validate(
                {
                    **base,
                    "events": [
                        {**call, "call_id": "z"},
                        intent,
                        new,
                        decision,
                        {**result, "seq": 5, "call_id": "z"},
                    ],
                }
            )

    def test_verify_reports_an_undeviable_source_as_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"trace_id": "t", "events": []}))  # no schema_version
        assert main(["verify", str(bad)]) == 2
        assert "schema_version" in capsys.readouterr().err


class TestSecondReviewFindings:
    def test_read_invariant_checks_recorded_head_and_cursor(self, journal_path: str) -> None:
        t = derive(journal_path)
        assert {r.name: r.status for r in check(t).results}[
            "read_observed_the_recorded_head"
        ] == "pass"
        doc = t.model_dump(mode="json")
        rr = next(e for e in doc["events"] if e["type"] == "read_result")
        rr["head"] = "e" * 64
        bad = check(TraceV2.model_validate(doc))
        assert {r.name: r.status for r in bad.results}["read_observed_the_recorded_head"] == "fail"
        doc2 = t.model_dump(mode="json")
        next(e for e in doc2["events"] if e["type"] == "read_result")["cursor"] = 0  # stale
        assert {r.name: r.status for r in check(TraceV2.model_validate(doc2)).results}[
            "read_observed_the_recorded_head"
        ] == "fail"

    def test_recorded_heads_must_chain(self, journal_path: str) -> None:
        t = derive(journal_path)
        doc = t.model_dump(mode="json")
        rejected = next(e for e in doc["events"] if e["type"] == "ledger_result" and not e["ok"])
        rejected["head"] = "d" * 64  # a rejection cannot move the head
        card = check(TraceV2.model_validate(doc))
        assert {r.name: r.status for r in card.results}[
            "books_balance_and_chain_verifies"
        ] == "fail"

    def test_chartless_v1_trace_reports_no_evidence_for_replay_not_a_traceback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rec = Recorder("t", AgentDoc(name="a"), CHART, SteppingClock(EPOCH), SequentialIds())
        rec.execute(OpenTransaction("o", "t", Money(1, USD)))
        doc = json.loads(dump_trace(rec.trace()))
        doc.pop("chart")
        p = tmp_path / "nochart.json"
        p.write_text(json.dumps(doc))
        assert main(["verify", str(p), "--json"]) == 0
        card = json.loads(capsys.readouterr().out)
        statuses = {i["name"]: i["status"] for i in card["invariants"]}
        assert statuses["ledger_pairs_replay"] == "no_evidence"

    def test_duplicate_command_ids_are_refused_at_load(self) -> None:
        from ledgergate.codec import decode_command
        from ledgergate.ledger import CURRENCIES, command_fingerprint

        at = EPOCH.isoformat()
        cmd = {"kind": "reverse", "key": "k", "entry_id": "e"}
        fp = command_fingerprint(decode_command(cmd, CURRENCIES))
        events = []
        for i, cid in enumerate(("dup", "dup")):
            base = i * 4
            events += [
                {
                    "type": "tool_call",
                    "seq": base + 1,
                    "at": at,
                    "call_id": f"c{i}",
                    "tool": "x",
                    "arguments": {},
                },
                {
                    "type": "legacy_intent",
                    "seq": base + 2,
                    "at": at,
                    "intent_id": f"i{i}",
                    "command": cmd,
                },
                {
                    "type": "invocation_resolution",
                    "seq": base + 3,
                    "at": at,
                    "intent_id": f"i{i}",
                    "disposition": "legacy",
                    "operation_id": cid,
                    "attempted_digest": fp,
                },
                {
                    "type": "ledger_command",
                    "seq": base + 4,
                    "at": at,
                    "command_id": cid,
                    "command": cmd,
                },
            ]
        events.append(
            {
                "type": "ledger_result",
                "seq": 9,
                "at": at,
                "command_id": "dup",
                "ok": False,
                "error": {"type": "X", "message": "m"},
                "head": "0" * 64,
                "sequence": 0,
            }
        )
        with pytest.raises(ValidationError, match="unique"):
            TraceV2.model_validate(
                {
                    "trace_id": "t",
                    "started_at": at,
                    "ended_at": at,
                    "policy_set_version": "legacy",
                    "events": events,
                }
            )

    def test_lift_accepts_a_maximal_v1_command_id(self) -> None:
        rec = Recorder("t", AgentDoc(name="a"), CHART, SteppingClock(EPOCH), SequentialIds())
        rec.execute(OpenTransaction("o", "t", Money(1, USD)))
        doc = json.loads(dump_trace(rec.trace()))
        long_id = "x" * 256
        for e in doc["events"]:
            if e["type"] in ("ledger_command", "ledger_result"):
                e["command_id"] = long_id
        lifted = load_any(json.dumps(doc))
        assert lifted.resolutions()[0].operation_id == long_id

    def test_derivation_is_one_snapshot(
        self, journal_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A row committed after derivation began is not seen by it."""
        import ledgergate.derive as derive_mod

        original = derive_mod._Derivation.run

        def run_and_write_midway(self: Any, trace_id: str | None) -> Any:
            j = Journal.open(
                journal_path,
                clock=SteppingClock(EPOCH + timedelta(days=1)),
                ids=SequentialIds(start=900),
                policy=POLICY,
            )
            j.record_message("user", "late")
            j.close()
            return original(self, trace_id)

        monkeypatch.setattr(derive_mod._Derivation, "run", run_and_write_midway)
        before = derive(journal_path)
        monkeypatch.undo()
        after = derive(journal_path)
        assert sum(e.type == "message" for e in before.events) == 1
        assert sum(e.type == "message" for e in after.events) == 2


class TestThirdReviewFindings:
    def test_a_schema_3_journal_is_refused_by_open_and_derive(self, journal_path: str) -> None:
        import sqlite3

        from ledgergate.journal import ConfigurationError

        conn = sqlite3.connect(journal_path, isolation_level=None)
        conn.execute("DROP TRIGGER definition_no_update")
        conn.execute("UPDATE definition SET schema_version = 3")
        conn.close()
        with pytest.raises(ConfigurationError, match="schema 3"):
            Journal.open(
                journal_path, clock=SteppingClock(EPOCH), ids=SequentialIds(), policy=POLICY
            )
        with pytest.raises(DerivationError, match="schema 3"):
            derive(journal_path)

    def test_dangling_references_are_refused_at_load(self, journal_path: str) -> None:
        t = derive(journal_path)
        doc = t.model_dump(mode="json")
        replay = next(
            e
            for e in doc["events"]
            if e["type"] == "invocation_resolution" and e["disposition"] == "replay"
        )
        replay["operation_id"] = "command-424242"
        with pytest.raises(ValidationError, match="unknown operation"):
            TraceV2.model_validate(doc)
        doc = t.model_dump(mode="json")
        replay = next(
            e
            for e in doc["events"]
            if e["type"] == "invocation_resolution" and e["disposition"] == "replay"
        )
        replay["outcome_ref"] = "outcome-999999"
        with pytest.raises(ValidationError, match="current outcome"):
            TraceV2.model_validate(doc)
        doc = t.model_dump(mode="json")
        news = [
            e
            for e in doc["events"]
            if e["type"] == "invocation_resolution" and e["disposition"] == "new"
        ]
        news[1]["outcome_ref"] = news[0]["outcome_ref"]
        with pytest.raises(ValidationError, match="already produced"):
            TraceV2.model_validate(doc)

    def test_runtime_decision_with_a_consumption_or_foreign_presentation_fails(
        self, tmp_path: Path
    ) -> None:
        j = Journal.create(
            str(tmp_path / "f.journal"),
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=POLICY,
            approval_key=verification_key_text(SIGNER),
        )
        j.handle(_open("big", "c1", 500))
        j.handle(
            _open(
                "big",
                "c2",
                500,
                approval=_artefact(j, "big", expires_at=EPOCH + timedelta(seconds=1)),
            )
        )
        j.close()
        t = derive(str(tmp_path / "f.journal"))
        doc = t.model_dump(mode="json")
        d = next(
            e
            for e in doc["events"]
            if e["type"] == "policy_decision" and e["matched_rule"].startswith("runtime.")
        )
        d["consumption_ref"] = "consumption-99"
        card = check(TraceV2.model_validate(doc))
        assert {r.name: r.status for r in card.results}["runtime_decisions_are_verdicts"] == "fail"
        doc = t.model_dump(mode="json")
        d = next(
            e
            for e in doc["events"]
            if e["type"] == "policy_decision" and e["matched_rule"].startswith("runtime.")
        )
        d["context"]["digest_kind"] = "request"
        card = check(TraceV2.model_validate(doc))
        assert {r.name: r.status for r in card.results}["context_matches_decision"] == "fail"


class TestFourthReviewFindings:
    def test_malformed_derived_references_are_refused_at_load_not_tracebacked(
        self, journal_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        doc = derive(journal_path).model_dump(mode="json")
        replay = next(
            e
            for e in doc["events"]
            if e["type"] == "invocation_resolution" and e["disposition"] == "replay"
        )
        replay["outcome_ref"] = "garbage"
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(doc))
        assert main(["verify", str(p)]) == 2
        assert "outcome_ref" in capsys.readouterr().err

    def test_produced_outcomes_must_follow_allocation_order(self, journal_path: str) -> None:
        doc = derive(journal_path).model_dump(mode="json")
        news = [
            e
            for e in doc["events"]
            if e["type"] == "invocation_resolution" and e["disposition"] == "new"
        ]
        news[-1]["outcome_ref"] = "outcome-1"  # a fresh number below everything produced before
        with pytest.raises(ValidationError, match="allocation order"):
            TraceV2.model_validate(doc)

    def test_read_ahead_of_the_books_fails(self, journal_path: str) -> None:
        doc = derive(journal_path).model_dump(mode="json")
        next(e for e in doc["events"] if e["type"] == "read_result")["cursor"] = 10_000
        card = check(TraceV2.model_validate(doc))
        assert {r.name: r.status for r in card.results}["read_observed_the_recorded_head"] == "fail"

    def test_a_failed_verdict_decided_by_policy_fails_verify(self, tmp_path: Path) -> None:
        j = Journal.create(
            str(tmp_path / "f.journal"),
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=POLICY,
            approval_key=verification_key_text(SIGNER),
        )
        j.handle(_open("big", "c1", 500))
        j.handle(
            _open(
                "big",
                "c2",
                500,
                approval=_artefact(j, "big", expires_at=EPOCH + timedelta(seconds=1)),
            )
        )
        j.close()
        doc = derive(str(tmp_path / "f.journal")).model_dump(mode="json")
        d = next(
            e
            for e in doc["events"]
            if e["type"] == "policy_decision" and e["matched_rule"].startswith("runtime.")
        )
        d["matched_rule"] = "p.deny_above"  # pretend the set decided it ...
        res = next(
            e
            for e in doc["events"]
            if e["type"] == "invocation_resolution" and e["intent_id"] == d["intent_id"]
        )
        res["outcome_ref"] = "outcome-999999"  # ... and produced a fresh outcome, so it loads
        card = check(TraceV2.model_validate(doc))
        assert {r.name: r.status for r in card.results}["runtime_decisions_are_verdicts"] == "fail"

    def test_an_approval_without_a_presentation_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="defined by a presented artefact"):
            InvocationResolution(
                seq=1,
                at=EPOCH,
                intent_id="i",
                disposition="approval",
                operation_id="op",
                outcome_ref="outcome-1",
                attempted_digest="0" * 64,
            )


class TestFifthReviewFindings:
    def _failed_approval_journal(self, tmp_path: Path) -> str:
        path = str(tmp_path / "fa.journal")
        j = Journal.create(
            path,
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=POLICY,
            approval_key=verification_key_text(SIGNER),
        )
        j.handle(_open("big", "c1", 500))
        j.handle(
            _open(
                "big",
                "c2",
                500,
                approval=_artefact(j, "big", expires_at=EPOCH + timedelta(seconds=1)),
            )
        )
        j.handle(_open("big", "c3", 500, approval=_artefact(j, "big", approval_id="good")))
        j.handle(_open("big", "c4", 500))
        j.close()
        return path

    def test_stripping_the_verdict_from_a_runtime_decision_fails(self, tmp_path: Path) -> None:
        doc = derive(self._failed_approval_journal(tmp_path)).model_dump(mode="json")
        d = next(
            e
            for e in doc["events"]
            if e["type"] == "policy_decision" and e["matched_rule"].startswith("runtime.")
        )
        d.pop("approval")
        d["context"]["approval"] = None
        card = check(TraceV2.model_validate(doc))
        assert not card.passed
        assert {r.name: r.status for r in card.results}["runtime_decisions_are_verdicts"] == "fail"

    def test_a_consumption_without_any_approval_fails(self, journal_path: str) -> None:
        doc = derive(journal_path).model_dump(mode="json")
        d = next(
            e for e in doc["events"] if e["type"] == "policy_decision" and e.get("approval") is None
        )
        d["consumption_ref"] = f"consumption-{_n(d['intent_id']) + 1}"  # inside its window
        card = check(TraceV2.model_validate(doc))
        assert {r.name: r.status for r in card.results}["runtime_decisions_are_verdicts"] == "fail"

    def test_a_decision_presentation_must_be_its_own_invocations(self, journal_path: str) -> None:
        doc = derive(journal_path).model_dump(mode="json")
        d = next(
            e for e in doc["events"] if e["type"] == "policy_decision" and e.get("approval") is None
        )
        d["approval"] = {
            "presentation_ref": "presentation-999",
            "verdict": "approval_not_applicable",
        }
        d["context"]["approval"] = {"presentation": 999, "verdict": "approval_not_applicable"}
        card = check(TraceV2.model_validate(doc))
        assert {r.name: r.status for r in card.results}["runtime_decisions_are_verdicts"] == "fail"

    def test_a_replay_must_name_the_outcome_current_at_the_time(self, tmp_path: Path) -> None:
        t = derive(self._failed_approval_journal(tmp_path))
        new, failed, approved, replay = t.resolutions()
        assert failed.outcome_ref == new.outcome_ref and replay.outcome_ref == approved.outcome_ref
        doc = t.model_dump(mode="json")
        res = [e for e in doc["events"] if e["type"] == "invocation_resolution"]
        res[3]["outcome_ref"] = res[0]["outcome_ref"]  # claim the retry was told "awaiting"
        with pytest.raises(ValidationError, match="current outcome"):
            TraceV2.model_validate(doc)
        doc = t.model_dump(mode="json")
        res = [e for e in doc["events"] if e["type"] == "invocation_resolution"]
        res[1]["outcome_ref"], res[1]["seq"] = res[2]["outcome_ref"], res[1]["seq"]
        with pytest.raises(ValidationError, match="current outcome"):
            TraceV2.model_validate(doc)

    def test_derivation_refuses_a_journal_with_two_presentations_for_one_invocation(
        self, tmp_path: Path
    ) -> None:
        import sqlite3

        path = self._failed_approval_journal(tmp_path)
        conn = sqlite3.connect(path, isolation_level=None)
        (pres,) = conn.execute(
            "SELECT * FROM approvals ORDER BY journal_sequence LIMIT 1"
        ).fetchall()
        conn.execute("BEGIN")
        seq = conn.execute("INSERT INTO journal (kind) VALUES ('approvals')").lastrowid
        conn.execute(
            "INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (seq, *pres[1:])
        )
        conn.execute("COMMIT")
        conn.close()
        with pytest.raises(DerivationError, match="presentations"):
            derive(path)


class TestSixthReviewFindings:
    def _denied_then_approved(self, tmp_path: Path) -> dict[str, Any]:
        """A doctored trace: the operation's only outcome is a policy deny, then an approval
        intent claims to have applied it."""
        path = str(tmp_path / "d.journal")
        j = Journal.create(
            path,
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=POLICY,
            approval_key=verification_key_text(SIGNER),
        )
        j.handle(_open("big", "c1", 500))
        j.handle(_open("big", "c2", 500, approval=_artefact(j, "big")))
        j.close()
        doc = derive(path).model_dump(mode="json")
        first = next(e for e in doc["events"] if e["type"] == "policy_decision")
        first["decision"] = "deny"
        first["matched_rule"], first["reason"] = "p.deny_above", "too big"
        return doc

    def test_an_approval_against_a_denied_operation_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="non-pending operation"):
            TraceV2.model_validate(self._denied_then_approved(tmp_path))

    def test_denied_never_reaches_ledger_is_operation_scoped(self, tmp_path: Path) -> None:
        """Through the registry alone (the grammar already refuses this shape at load): a deny
        on an operation followed by a ledger command for that operation fails the row."""
        from ledgergate.invariants import denied_never_reaches_ledger

        doc = self._denied_then_approved(tmp_path)
        # a model built without validation, so the invariant, not the grammar, must catch it
        built = TraceV2.model_construct(**doc)
        object.__setattr__(built, "events", tuple(_to_event(e) for e in doc["events"]))
        findings = denied_never_reaches_ledger(built)
        assert any("operation that was denied" in f.message for f in findings)

    def test_command_and_digest_must_agree(self, journal_path: str) -> None:
        t = derive(journal_path)
        doc = t.model_dump(mode="json")
        intent = next(e for e in doc["events"] if e["type"] == "command_intent")
        pair = next(
            e
            for e in doc["events"]
            if e["type"] == "ledger_command" and e["command"] == intent["command"]
        )
        intent["command"]["draft"]["postings"][0]["money"]["amount"] = 1
        intent["command"]["draft"]["postings"][1]["money"]["amount"] = 1
        with pytest.raises(ValidationError, match="not the command's fingerprint"):
            TraceV2.model_validate(doc)
        doc = t.model_dump(mode="json")
        pair = next(e for e in doc["events"] if e["type"] == "ledger_command")
        pair["command"]["draft"]["postings"][0]["money"]["amount"] = 1
        pair["command"]["draft"]["postings"][1]["money"]["amount"] = 1
        with pytest.raises(ValidationError, match="ledger_command differs"):
            TraceV2.model_validate(doc)

    def test_a_replay_must_carry_the_operations_command(self, journal_path: str) -> None:
        t = derive(journal_path)
        doc = t.model_dump(mode="json")
        replay_res = next(
            e
            for e in doc["events"]
            if e["type"] == "invocation_resolution" and e["disposition"] == "replay"
        )
        intent = next(
            e
            for e in doc["events"]
            if e["type"] == "command_intent" and e["intent_id"] == replay_res["intent_id"]
        )
        intent["command"]["draft"]["description"] = "changed"
        with pytest.raises(ValidationError, match="not the command's fingerprint"):
            TraceV2.model_validate(doc)

    def test_a_stray_boundary_pair_is_refused_in_a_runtime_trace(self, journal_path: str) -> None:
        doc = derive(journal_path).model_dump(mode="json")
        n = len(doc["events"])
        at = doc["events"][-1]["at"]
        doc["events"] += [
            {
                "type": "tool_call",
                "seq": n + 1,
                "at": at,
                "call_id": "ghost",
                "tool": "post",
                "arguments": {"amount": 999999},
            },
            {
                "type": "tool_result",
                "seq": n + 2,
                "at": at,
                "call_id": "ghost",
                "ok": True,
                "result": {},
            },
        ]
        with pytest.raises(ValidationError, match="brackets no intent"):
            TraceV2.model_validate(doc)

    def test_a_journal_without_a_response_row_is_a_derivation_error(
        self, journal_path: str
    ) -> None:
        import sqlite3

        conn = sqlite3.connect(journal_path, isolation_level=None)
        conn.execute("DROP TRIGGER invocation_responses_no_delete")
        conn.execute(
            "DELETE FROM invocation_responses WHERE journal_sequence ="
            " (SELECT MAX(journal_sequence) FROM invocation_responses)"
        )
        conn.close()
        with pytest.raises(DerivationError, match="no response row"):
            derive(journal_path)

    def test_one_decision_per_invocation_by_schema(self, journal_path: str) -> None:
        import sqlite3

        conn = sqlite3.connect(journal_path, isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        (dec,) = conn.execute(
            "SELECT * FROM decisions ORDER BY journal_sequence LIMIT 1"
        ).fetchall()
        conn.execute("BEGIN")
        seq = conn.execute("INSERT INTO journal (kind) VALUES ('decisions')").lastrowid
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)", (seq, *dec[1:]))
        conn.execute("ROLLBACK")
        conn.close()


def _to_event(doc: dict[str, Any]) -> Any:
    from pydantic import TypeAdapter

    from ledgergate.trace.v2 import V2Event

    return TypeAdapter(V2Event).validate_python(doc)


class TestSeventhReviewFindings:
    def test_legacy_rows_cannot_be_smuggled_into_a_derived_trace(self, journal_path: str) -> None:
        doc = derive(journal_path).model_dump(mode="json")
        n = len(doc["events"])
        at = doc["events"][-1]["at"]
        cmd = {
            "kind": "post",
            "key": "smuggled",
            "draft": {
                "postings": [
                    {
                        "account": "cash",
                        "side": "debit",
                        "money": {"amount": 999999, "currency": "USD"},
                    },
                    {
                        "account": "revenue",
                        "side": "credit",
                        "money": {"amount": 999999, "currency": "USD"},
                    },
                ]
            },
        }
        doc["events"] += [
            {
                "type": "legacy_intent",
                "seq": n + 1,
                "at": at,
                "intent_id": "legacy-1",
                "command": cmd,
            },
            {
                "type": "invocation_resolution",
                "seq": n + 2,
                "at": at,
                "intent_id": "legacy-1",
                "disposition": "legacy",
                "operation_id": "smuggled",
                "attempted_digest": "0" * 64,
            },
            {
                "type": "ledger_command",
                "seq": n + 3,
                "at": at,
                "command_id": "smuggled",
                "command": cmd,
            },
            {
                "type": "ledger_result",
                "seq": n + 4,
                "at": at,
                "command_id": "smuggled",
                "ok": False,
                "error": {"type": "X", "message": "m"},
                "head": "0" * 64,
                "sequence": 0,
            },
        ]
        with pytest.raises(ValidationError, match="no legacy content"):
            TraceV2.model_validate(doc)
        doc.pop("journal_id")
        with pytest.raises(ValidationError, match="only legacy dispositions"):
            TraceV2.model_validate(doc)

    def test_ledger_pair_is_tied_to_its_operation_and_call(self, journal_path: str) -> None:
        doc = derive(journal_path).model_dump(mode="json")
        pair = next(e for e in doc["events"] if e["type"] == "ledger_command")
        result = next(
            e
            for e in doc["events"]
            if e["type"] == "ledger_result" and e["command_id"] == pair["command_id"]
        )
        pair["command_id"] = result["command_id"] = "command-999"
        with pytest.raises(ValidationError, match="names another operation or call"):
            TraceV2.model_validate(doc)
        doc = derive(journal_path).model_dump(mode="json")
        next(e for e in doc["events"] if e["type"] == "ledger_command")["call_id"] = "somebody-else"
        with pytest.raises(ValidationError, match="names another operation or call"):
            TraceV2.model_validate(doc)

    def test_a_consumption_referenced_twice_fails(self, tmp_path: Path) -> None:
        path = str(tmp_path / "two.journal")
        j = Journal.create(
            path,
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=POLICY,
            approval_key=verification_key_text(SIGNER),
        )
        for key in ("k1", "k2"):
            j.handle(
                {
                    "tool": "open_transaction",
                    "call_id": f"c-{key}",
                    "key": key,
                    "arguments": {
                        "transaction_id": key,
                        "amount": {"amount": 500, "currency": "USD"},
                    },
                }
            )
        for key in ("k1", "k2"):
            j.handle(
                {
                    "tool": "open_transaction",
                    "call_id": f"a-{key}",
                    "key": key,
                    "approval": _artefact(j, key),
                    "arguments": {
                        "transaction_id": key,
                        "amount": {"amount": 500, "currency": "USD"},
                    },
                }
            )
        j.close()
        doc = derive(path).model_dump(mode="json")
        approvals = [
            e for e in doc["events"] if e["type"] == "policy_decision" and e.get("consumption_ref")
        ]
        assert len(approvals) == 2
        approvals[1]["consumption_ref"] = approvals[0]["consumption_ref"]
        # the model refuses it first: another invocation's row cannot be this one's
        with pytest.raises(ValidationError, match="not written by this invocation"):
            TraceV2.model_validate(doc)
        # and the registry catches the same shape on a model built without validation
        from ledgergate.invariants import runtime_decisions_are_verdicts

        built = TraceV2.model_construct(**doc)
        object.__setattr__(built, "events", tuple(_to_event(e) for e in doc["events"]))
        assert any(
            "more than one decision" in f.message for f in runtime_decisions_are_verdicts(built)
        )


class TestEighthReviewFindings:
    def test_a_v1_document_with_tool_events_and_no_ledger_command_lifts(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rec = Recorder("t", AgentDoc(name="a"), CHART, SteppingClock(EPOCH), SequentialIds())
        rec.message("user", "what is my balance")
        rec.tool_call("c1", "balance", {"account": "cash"})
        rec.tool_result("c1", True, {"balance": "0"})
        lifted = lift(rec.trace())
        assert [e.type for e in lifted.events] == ["message", "tool_call", "tool_result"]
        only_messages = Recorder(
            "t", AgentDoc(name="a"), CHART, SteppingClock(EPOCH), SequentialIds()
        )
        only_messages.message("user", "hi")
        assert len(lift(only_messages.trace()).events) == 1
        p = tmp_path / "obs.json"
        p.write_text(dump_trace(rec.trace()))
        assert main(["verify", str(p), "--json"]) == 3  # nothing to check is never a pass
        card = json.loads(capsys.readouterr().out)
        assert card["intents"] == 0 and card["status"] == "no_evidence" and not card["passed"]
        assert main(["verify", str(p)]) == 3
        assert "NO_EVIDENCE" in capsys.readouterr().out

    def test_a_runtime_document_must_carry_its_journal_id(self, journal_path: str) -> None:
        doc = derive(journal_path).model_dump(mode="json")
        doc.pop("journal_id")
        with pytest.raises(ValidationError, match="only legacy dispositions"):
            TraceV2.model_validate(doc)

    def test_an_approval_that_consumed_nothing_fails_verify(self, tmp_path: Path) -> None:
        path = str(tmp_path / "nc.journal")
        j = Journal.create(
            path,
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=POLICY,
            approval_key=verification_key_text(SIGNER),
        )
        j.handle(_open("big", "c1", 500))
        j.handle(_open("big", "c2", 500, approval=_artefact(j, "big")))
        j.close()
        t = derive(path)
        doc = t.model_dump(mode="json")
        d = next(
            e for e in doc["events"] if e["type"] == "policy_decision" and e.get("consumption_ref")
        )
        d["approval"]["verdict"] = "approval_not_applicable"
        d["context"]["approval"]["verdict"] = "approval_not_applicable"
        d.pop("consumption_ref")
        card = check(TraceV2.model_validate(doc))
        assert {r.name: r.status for r in card.results}["runtime_decisions_are_verdicts"] == "fail"
        # and a `new` cannot claim to have consumed a valid artefact
        doc = t.model_dump(mode="json")
        new = next(
            e
            for e in doc["events"]
            if e["type"] == "policy_decision" and e["decision"] == "approval_required"
        )
        res = next(
            e
            for e in doc["events"]
            if e["type"] == "invocation_resolution" and e["intent_id"] == new["intent_id"]
        )
        n = _n(new["intent_id"])
        res["presentation_ref"] = f"presentation-{n + 1}"
        new["approval"] = {"presentation_ref": f"presentation-{n + 1}", "verdict": "approval_valid"}
        new["context"]["approval"] = {"presentation": n + 1, "verdict": "approval_valid"}
        new["consumption_ref"] = f"consumption-{n + 2}"
        card = check(TraceV2.model_validate(doc))
        assert {r.name: r.status for r in card.results}["runtime_decisions_are_verdicts"] == "fail"

    def test_a_consumption_before_its_presentation_fails(self, tmp_path: Path) -> None:
        path = str(tmp_path / "order.journal")
        j = Journal.create(
            path,
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=POLICY,
            approval_key=verification_key_text(SIGNER),
        )
        j.handle(_open("big", "c1", 500))
        j.handle(_open("big", "c2", 500, approval=_artefact(j, "big")))
        j.close()
        doc = derive(path).model_dump(mode="json")
        d = next(
            e for e in doc["events"] if e["type"] == "policy_decision" and e.get("consumption_ref")
        )
        d["consumption_ref"] = f"consumption-{_n(d['intent_id']) + 1}"  # before its presentation
        card = check(TraceV2.model_validate(doc))
        assert {r.name: r.status for r in card.results}["runtime_decisions_are_verdicts"] == "fail"


class TestNinthReviewFindings:
    def test_derived_references_are_anchored_to_their_invocation(self, journal_path: str) -> None:
        t = derive(journal_path)
        doc = t.model_dump(mode="json")
        res = [e for e in doc["events"] if e["type"] == "invocation_resolution"]
        # renumber outcomes from 1: a new at intent-n cannot have produced outcome-1
        produced = [r for r in res if r["disposition"] == "new"]
        for i, r in enumerate(produced):
            r["outcome_ref"] = f"outcome-{i + 1}"
        for r in res:
            if r["disposition"] == "replay":
                r["outcome_ref"] = produced[0]["outcome_ref"]
        with pytest.raises(ValidationError, match="not written by this invocation"):
            TraceV2.model_validate(doc)
        doc = t.model_dump(mode="json")
        for e in doc["events"]:
            if e.get("presentation_ref"):
                e["presentation_ref"] = "presentation-1"
            if e.get("approval"):
                e["approval"]["presentation_ref"] = "presentation-1"
        with pytest.raises(ValidationError, match="not written by this invocation"):
            TraceV2.model_validate(doc)
        doc = t.model_dump(mode="json")
        ids = [e for e in doc["events"] if e.get("intent_id")]
        first, second = (
            ids[0]["intent_id"],
            next(e["intent_id"] for e in ids if e["intent_id"] != ids[0]["intent_id"]),
        )
        for e in doc["events"]:
            if e.get("intent_id") == first:
                e["intent_id"] = "intent-999999"
        with pytest.raises(ValidationError, match="strictly increase"):
            TraceV2.model_validate(doc)
        del second

    def test_a_message_interleaved_inside_an_intent_is_refused(self, journal_path: str) -> None:
        doc = derive(journal_path).model_dump(mode="json")
        i = next(i for i, e in enumerate(doc["events"]) if e["type"] == "command_intent")
        doc["events"].insert(
            i + 1,
            {
                "type": "message",
                "seq": 0,
                "at": doc["events"][i]["at"],
                "role": "user",
                "content": "x",
            },
        )
        for n, e in enumerate(doc["events"]):
            e["seq"] = n + 1
        with pytest.raises(ValidationError, match="interleaved"):
            TraceV2.model_validate(doc)

    def test_a_lifted_intent_digest_is_rechecked_on_load(self) -> None:
        rec = Recorder("t", AgentDoc(name="a"), CHART, SteppingClock(EPOCH), SequentialIds())
        rec.execute(OpenTransaction("o", "t", Money(1, USD)))
        doc = lift(rec.trace()).model_dump(mode="json")
        next(e for e in doc["events"] if e["type"] == "invocation_resolution")[
            "attempted_digest"
        ] = "0" * 64
        with pytest.raises(ValidationError, match="not the command's fingerprint"):
            TraceV2.model_validate(doc)


def _n(ref: str) -> int:
    return int(ref.rsplit("-", 1)[1])


class TestTenthReviewFindings:
    def _statuses(self, doc: dict[str, Any]) -> dict[str, str]:
        return {r.name: r.status for r in check(TraceV2.model_validate(doc)).results}

    def test_caller_told_applied_on_a_pending_intent_fails(self, journal_path: str) -> None:
        t = derive(journal_path)
        assert {r.name: r.status for r in check(t).results}[
            "caller_was_told_what_happened"
        ] == "pass"
        doc = t.model_dump(mode="json")
        awaiting = next(
            e
            for e in doc["events"]
            if e["type"] == "policy_decision" and e["decision"] == "approval_required"
        )
        events = doc["events"]
        i = next(i for i, e in enumerate(events) if e.get("intent_id") == awaiting["intent_id"])
        tr = next(e for e in events[i:] if e["type"] == "tool_result")
        tr.update({"ok": True, "result": {"faked": "yes"}})
        tr.pop("error", None)
        assert self._statuses(doc)["caller_was_told_what_happened"] == "fail"

    def test_replay_must_be_told_what_the_producer_was_told(self, journal_path: str) -> None:
        doc = derive(journal_path).model_dump(mode="json")
        events = doc["events"]
        replay = next(
            e
            for e in events
            if e["type"] == "invocation_resolution" and e["disposition"] == "replay"
        )
        i = events.index(replay)
        tr = next(e for e in events[i:] if e["type"] == "tool_result")
        tr.update({"ok": False, "error": {"type": "PolicyDenied", "message": "no"}})
        tr.pop("result", None)
        assert self._statuses(doc)["caller_was_told_what_happened"] == "fail"

    def test_a_served_balance_must_match_the_reads_digest(self, journal_path: str) -> None:
        t = derive(journal_path)
        assert {r.name: r.status for r in check(t).results}[
            "read_result_binds_the_served_value"
        ] == "pass"
        doc = t.model_dump(mode="json")
        events = doc["events"]
        rr = next(e for e in events if e["type"] == "read_result")
        i = events.index(rr)
        tr = events[i + 1]
        assert tr["type"] == "tool_result"
        tr["result"] = {**tr["result"], "balance": "999999"}
        assert self._statuses(doc)["read_result_binds_the_served_value"] == "fail"

    def test_a_replay_with_an_artefact_against_a_pending_operation_is_refused(
        self, tmp_path: Path
    ) -> None:
        path = str(tmp_path / "ra.journal")
        j = Journal.create(
            path,
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=POLICY,
            approval_key=verification_key_text(SIGNER),
        )
        j.handle(_open("big", "c1", 500))
        j.handle(_open("big", "c2", 500))  # honest replay, no artefact
        j.close()
        doc = derive(path).model_dump(mode="json")
        replay = next(
            e
            for e in doc["events"]
            if e["type"] == "invocation_resolution" and e["disposition"] == "replay"
        )
        replay["presentation_ref"] = f"presentation-{_n(replay['intent_id']) + 1}"
        with pytest.raises(ValidationError, match="is an approval"):
            TraceV2.model_validate(doc)

    def test_a_lifted_pair_must_carry_the_intents_command(self) -> None:
        rec = Recorder("t", AgentDoc(name="a"), CHART, SteppingClock(EPOCH), SequentialIds())
        rec.execute(OpenTransaction("o", "t", Money(1, USD)))
        doc = lift(rec.trace()).model_dump(mode="json")
        next(e for e in doc["events"] if e["type"] == "ledger_command")["command"]["amount"][
            "amount"
        ] = 2
        with pytest.raises(ValidationError, match="ledger_command differs"):
            TraceV2.model_validate(doc)


class TestEleventhReviewFindings:
    def _statuses(self, doc: dict[str, Any]) -> dict[str, str]:
        return {r.name: r.status for r in check(TraceV2.model_validate(doc)).results}

    def _replay_result(self, doc: dict[str, Any]) -> dict[str, Any]:
        events = doc["events"]
        replay = next(
            e
            for e in events
            if e["type"] == "invocation_resolution" and e["disposition"] == "replay"
        )
        return next(e for e in events[events.index(replay) :] if e["type"] == "tool_result")

    def test_replay_must_be_told_exactly_what_the_producer_was_told(
        self, journal_path: str
    ) -> None:
        t = derive(journal_path)
        doc = t.model_dump(mode="json")
        tr = self._replay_result(doc)
        assert tr["ok"] and tr["result"]["replayed"] is True
        tr["result"]["head"] = "f" * 64
        assert self._statuses(doc)["caller_was_told_what_happened"] == "fail"
        doc = t.model_dump(mode="json")
        self._replay_result(doc)["result"].pop("replayed")
        assert self._statuses(doc)["caller_was_told_what_happened"] == "fail"

    def test_denial_message_must_carry_rule_and_reason(self, journal_path: str) -> None:
        doc = derive(journal_path).model_dump(mode="json")
        events = doc["events"]
        awaiting = next(
            e
            for e in events
            if e["type"] == "policy_decision" and e["decision"] == "approval_required"
        )
        i = next(i for i, e in enumerate(events) if e.get("intent_id") == awaiting["intent_id"])
        tr = next(e for e in events[i:] if e["type"] == "tool_result")
        tr["error"]["message"] = "some.other_rule: swapped"
        assert self._statuses(doc)["caller_was_told_what_happened"] == "fail"

    def test_applied_write_must_serve_the_ledger_results_head(self, journal_path: str) -> None:
        doc = derive(journal_path).model_dump(mode="json")
        events = doc["events"]
        lr = next(e for e in events if e["type"] == "ledger_result" and e["ok"])
        tr = events[events.index(lr) + 1]
        assert tr["type"] == "tool_result" and tr["result"]["head"] == lr["head"]
        tr["result"]["head"] = "e" * 64
        assert self._statuses(doc)["caller_was_told_what_happened"] == "fail"


class TestTwelfthReviewFindings:
    def test_denied_read_message_must_carry_rule_and_reason(self, tmp_path: Path) -> None:
        from ledgergate.journal.policy import Decision, PolicyContext

        class DenyReads(ThresholdPolicySet):
            def evaluate(self, context: PolicyContext) -> Decision:
                if context.digest_kind == "request":
                    return Decision("deny", f"{self.version}.no_reads", "reads are refused")
                return super().evaluate(context)

        path = str(tmp_path / "dr.journal")
        j = Journal.create(
            path,
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=DenyReads(version="d", gated_reads=frozenset({"balance"})),
        )
        j.handle({"tool": "balance", "call_id": "c", "arguments": {"account": "cash"}})
        j.close()
        doc = derive(path).model_dump(mode="json")
        tr = next(e for e in doc["events"] if e["type"] == "tool_result")
        assert tr["error"]["message"] == "d.no_reads: reads are refused"
        tr["error"]["message"] = "other.rule: swapped"
        card = check(TraceV2.model_validate(doc))
        assert {r.name: r.status for r in card.results}["caller_was_told_what_happened"] == "fail"


class TestPostApprovalTightening:
    def test_scalar_replay_results_are_compared(self, journal_path: str) -> None:
        from ledgergate.invariants import caller_was_told_what_happened

        doc = derive(journal_path).model_dump(mode="json")
        events = doc["events"]
        new_ok = next(e for e in events if e["type"] == "ledger_result" and e["ok"])
        producer_tr = events[events.index(new_ok) + 1]
        replay = next(
            e
            for e in events
            if e["type"] == "invocation_resolution" and e["disposition"] == "replay"
        )
        replay_tr = next(e for e in events[events.index(replay) :] if e["type"] == "tool_result")
        producer_tr["result"], replay_tr["result"] = 1, 2
        built = TraceV2.model_construct(**doc)
        object.__setattr__(built, "events", tuple(_to_event(e) for e in events))
        assert any("not told exactly" in f.message for f in caller_was_told_what_happened(built))

    def test_rejected_write_served_error_is_the_ledger_results(self, journal_path: str) -> None:
        doc = derive(journal_path).model_dump(mode="json")
        events = doc["events"]
        lr = next(e for e in events if e["type"] == "ledger_result" and not e["ok"])
        tr = events[events.index(lr) + 1]
        assert tr["type"] == "tool_result" and tr["error"] == lr["error"]
        tr["error"]["message"] = "softened"
        card = check(TraceV2.model_validate(doc))
        assert {r.name: r.status for r in card.results}["caller_was_told_what_happened"] == "fail"

    def test_a_policy_set_naming_the_runtime_namespace_is_refused(self, tmp_path: Path) -> None:
        from ledgergate.journal import ConfigurationError
        from ledgergate.journal.policy import Decision, PolicyContext

        class Impostor(ThresholdPolicySet):
            def evaluate(self, context: PolicyContext) -> Decision:
                return Decision("deny", "runtime.approval_rejected", "approval_expired")

        j = Journal.create(
            str(tmp_path / "imp.journal"),
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=Impostor(version="imp"),
        )
        with pytest.raises(ConfigurationError, match="reserved"):
            j.handle(_open("k", "c", 5))
        j.close()
