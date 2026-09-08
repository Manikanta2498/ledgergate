"""Tests written against the first nightly's surviving mutants in the rows the 2026-09-06
review added (`attributions_are_registered`, `approval_evidence_is_consistent`): each test
names the mutant class it kills, so a survivor is a decision, not an accident."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from ledgergate.derive import trace as derive_trace
from ledgergate.invariants import Finding, check
from ledgergate.journal import Journal, generate_signing_key, issue, verification_key_text
from ledgergate.journal.auth import sign_request
from ledgergate.ledger import (
    EPOCH,
    USD,
    Account,
    AccountType,
    ChartOfAccounts,
    SequentialIds,
    SteppingClock,
)
from ledgergate.trace import dump_v2, load_any

CHART = ChartOfAccounts(
    [Account("cash", AccountType.ASSET, USD), Account("revenue", AccountType.REVENUE, USD)]
)
CFO = generate_signing_key()
AGENT = generate_signing_key()


def _post(key: str, call_id: str) -> dict[str, Any]:
    return {
        "tool": "post",
        "call_id": call_id,
        "key": key,
        "arguments": {
            "draft": {
                "postings": [
                    {"account": "cash", "side": "debit", "money": {"amount": 1, "currency": "USD"}},
                    {
                        "account": "revenue",
                        "side": "credit",
                        "money": {"amount": 1, "currency": "USD"},
                    },
                ]
            }
        },
    }


def _doc(j: Journal) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(dump_v2(derive_trace(j.path)))
    return doc


def _status(doc: dict[str, Any], row: str) -> tuple[str, list[str]]:
    card = check(load_any(json.dumps(doc)))
    r = next(x for x in card.results if x.name == row)
    return r.status, [f.message for f in r.findings]


def _reseq(doc: dict[str, Any]) -> dict[str, Any]:
    for i, e in enumerate(doc["events"], start=1):
        e["seq"] = i
    return doc


@pytest.fixture
def j(tmp_path: Path) -> Any:
    journal = Journal.create(
        str(tmp_path / "j.journal"),
        CHART,
        clock=SteppingClock(EPOCH),
        ids=SequentialIds(),
        approvers={"cfo": verification_key_text(CFO)},
    )
    journal.add_principal("agent", "signed", verification_key_text(AGENT))
    yield journal
    journal.close()


def _artefact(j: Journal, key: str) -> dict[str, Any]:
    return issue(
        CFO,
        journal_id=j.definition.journal_id,
        approval_id=f"appr-{key}",
        approver="cfo",
        fingerprint="0" * 64,
        key=key,
        issued_at=EPOCH,
        expires_at=EPOCH + timedelta(days=1),
    ).to_json()


class TestFindingContract:
    """Kills the Finding-shape mutants: a finding that misnames its row, its severity, its
    message or its intent is refused at construction, and `check` refuses a row's finding
    that is not a Finding of that row."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"invariant": "ATTRIBUTIONS_ARE_REGISTERED"},
            {"invariant": None},
            {"severity": "ERROR"},
            {"severity": None},
            {"message": None},
            {"message": ""},
            {"message": "intent-3: wrong", "intent_id": None},
            {"message": "intent-3: wrong", "intent_id": "intent-4"},
        ],
    )
    def test_a_mis_shaped_finding_is_refused(self, kwargs: dict[str, Any]) -> None:
        base: dict[str, Any] = {
            "invariant": "attributions_are_registered",
            "severity": "error",
            "message": "intent-3: wrong",
            "intent_id": "intent-3",
        }
        with pytest.raises(TypeError):
            Finding(**{**base, **kwargs})
        Finding(**base)  # the well-formed one is accepted

    def test_check_refuses_a_row_that_emits_another_rows_finding(self, j: Journal) -> None:
        from ledgergate import invariants

        j.handle(_post("k1", "c1"))
        t = derive_trace(j.path)
        import dataclasses

        registry = tuple(
            dataclasses.replace(
                inv, check=lambda _t: [Finding("books_balance_and_chain_verifies", "error", "x")]
            )
            if inv.name == "attributions_are_registered"
            else inv
            for inv in invariants.REGISTRY
        )
        with pytest.raises(TypeError, match="does not own"):
            check(t, registry)


class TestUncoveredSites:
    def test_a_rejected_envelope_on_a_session_that_is_not_live_fails(self, j: Journal) -> None:
        bad = {
            **_post("k1", "c1"),
            "auth": {
                "principal": "agent",
                "expires_at": "2026-01-01T00:00:00+00:00",
                "signature": "A" * 86,
            },
        }
        assert j.handle(bad).error_type == "bad_signature"
        doc = _doc(j)
        # forge: the rejected row's session principal is one that was never registered
        for e in doc["events"]:
            if e["type"] == "invocation_resolution" and e["authentication"] == "rejected":
                e["principal"] = "ghost"
        status, messages = _status(doc, "attributions_are_registered")
        assert status == "fail" and any("rejected envelope on a session" in m for m in messages)

    def test_a_verified_presentation_and_a_context_approver_that_are_not_live_fail(
        self, j: Journal
    ) -> None:
        # a read presenting a valid artefact: verified, approval_not_applicable
        assert j.handle(
            {
                "tool": "balance",
                "call_id": "r1",
                "arguments": {"account": "cash"},
                "approval": _artefact(j, "k1"),
            }
        ).ok
        doc = _doc(j)
        # honest: passes, including the not_applicable presentation (kills the uppercase
        # 'APPROVAL_NOT_APPLICABLE' mutants, which would demand an approver on it)
        assert _status(doc, "attributions_are_registered")[0] == "pass"
        assert _status(doc, "approval_evidence_is_consistent")[0] == "pass"
        # forge: the approver's add is removed, so the verified presentation names a name
        # that was never live (kills `e.approver is None` on the presentation branch)
        forged = json.loads(json.dumps(doc))
        forged["events"] = [
            e
            for e in forged["events"]
            if not (e["type"] == "approver_change" and e["name"] == "cfo")
        ]
        _reseq(forged)
        status, messages = _status(forged, "attributions_are_registered")
        assert status == "fail" and any(
            "verified presentation by cfo, not live" in m for m in messages
        )

    def test_a_context_approver_that_is_not_live_at_the_decision_fails(
        self, tmp_path: Path
    ) -> None:
        from ledgergate.journal import ThresholdPolicySet
        from ledgergate.journal.policy import Threshold

        policy = ThresholdPolicySet(
            version="v1", approve_above=[Threshold("open_transaction", "USD", 5_000)]
        )
        j = Journal.create(
            str(tmp_path / "g.journal"),
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=policy,
            approvers={"cfo": verification_key_text(CFO)},
        )
        try:
            opener = {
                "tool": "open_transaction",
                "call_id": "c1",
                "key": "k1",
                "arguments": {"transaction_id": "t", "amount": {"amount": 6000, "currency": "USD"}},
            }
            j.handle(opener)
            import sqlite3

            conn = sqlite3.connect(j.path)
            try:
                (fp,) = conn.execute("SELECT fingerprint FROM operations").fetchone()
            finally:
                conn.close()
            art = issue(
                CFO,
                journal_id=j.definition.journal_id,
                approval_id="a1",
                approver="cfo",
                fingerprint=fp,
                key="k1",
                issued_at=EPOCH,
                expires_at=EPOCH + timedelta(days=1),
            ).to_json()
            assert j.handle({**opener, "call_id": "c2", "approval": art}).response == "applied"
            doc = _doc(j)
        finally:
            j.close()
        # move the cfo add after the decision: the approver was not live when the decision
        # was made (kills the decision-liveness site and the `<` vs `<=` position mutants)
        forged = json.loads(json.dumps(doc))
        add = next(e for e in forged["events"] if e["type"] == "approver_change")
        forged["events"].remove(add)
        forged["events"].append(add)
        _reseq(forged)
        status, messages = _status(forged, "attributions_are_registered")
        assert status == "fail" and any("not live at the decision" in m for m in messages)


class TestBehaviouralSurvivors:
    def test_no_evidence_only_when_neither_presentations_nor_verdicts_exist(
        self, j: Journal
    ) -> None:
        # kills `is not None` -> `is None` in the needs-gate: plain decisions without an
        # approval are not approval evidence
        j.handle(_post("k1", "c1"))
        doc = _doc(j)
        assert _status(doc, "approval_evidence_is_consistent")[0] == "no_evidence"

    def test_a_revoked_principal_row_without_any_revoke_is_forged(self, j: Journal) -> None:
        # kills `== "revoke"` -> `!= "revoke"` and `and` -> `or` in the revoke lookup: a
        # transport row relabelled revoked_principal with no revoke anywhere before it
        j.handle({"tool": "nope", "call_id": "c1"})
        doc = _doc(j)
        for e in doc["events"]:
            if e["type"] == "invocation_resolution" and e["disposition"] == "invalid":
                e["error_type"] = "revoked_principal"
            if e["type"] == "tool_result" and not e["ok"]:
                e["error"]["type"] = "revoked_principal"
        status, messages = _status(doc, "attributions_are_registered")
        assert status == "fail" and any("was not revoked" in m for m in messages)


class TestSignedReadStillPasses:
    def test_round_trip(self, j: Journal) -> None:
        v = {"tool": "balance", "call_id": "r1", "arguments": {"account": "cash"}}
        v["auth"] = sign_request(
            v,
            private=AGENT,
            journal_id=j.definition.journal_id,
            principal="agent",
            expires_at=EPOCH + timedelta(days=1),
        )
        assert j.handle(v).ok
        assert check(derive_trace(j.path)).status == "pass"


class TestFindingMessagesSayWhy:
    """A finding's message is what an auditor reads: these pin the text at the sites whose
    message mutants survived (a `None` message renders as `intent-n: None`, which the shape
    contract cannot tell from prose)."""

    def _forge_and_expect(self, doc: dict[str, Any], row: str, needle: str, mutate: Any) -> None:
        forged = json.loads(json.dumps(doc))
        mutate(forged)
        status, messages = _status(forged, row)
        assert status == "fail", messages
        assert any(needle in m for m in messages), messages

    def test_attribution_messages(self, j: Journal) -> None:
        v = _post("k1", "c1")
        v["auth"] = sign_request(
            v,
            private=AGENT,
            journal_id=j.definition.journal_id,
            principal="agent",
            expires_at=EPOCH + timedelta(days=1),
        )
        assert j.handle(v).ok
        bad = {
            **_post("k2", "c2"),
            "auth": {
                "principal": "agent",
                "expires_at": "2026-01-01T00:00:00+00:00",
                "signature": "A" * 86,
            },
        }
        assert j.handle(bad).error_type == "bad_signature"
        doc = _doc(j)
        row = "attributions_are_registered"

        def resolution(pred: Any) -> Any:
            return lambda d: [
                e for e in d["events"] if e["type"] == "invocation_resolution" and pred(e)
            ]

        def set_on(pred: Any, **fields: Any) -> Any:
            def go(d: dict[str, Any]) -> None:
                for e in resolution(pred)(d):
                    e.update(fields)

            return go

        signed = lambda e: e["authentication"] == "signed"  # noqa: E731
        rejected = lambda e: e["authentication"] == "rejected"  # noqa: E731
        # cause/authentication matrix
        self._forge_and_expect(
            doc,
            row,
            "is reached under rejected authentication, the row says signed",
            set_on(rejected, authentication="signed", principal="agent"),
        )
        # rejected cause outside the clockless set
        self._forge_and_expect(
            doc,
            row,
            "a rejected envelope's cause is one of",
            lambda d: (
                [e.update({"error_type": "request_expired"}) for e in resolution(rejected)(d)]
                and [
                    e["error"].update({"type": "request_expired"})
                    for e in d["events"]
                    if e["type"] == "tool_result" and not e["ok"]
                ]
            ),
        )
        # kind liveness: the signed row names a transport principal
        self._forge_and_expect(
            doc, row, "is not live with that kind", set_on(signed, principal="local")
        )

        # the policy saw another principal
        def context(d: dict[str, Any]) -> None:
            for e in d["events"]:
                if e["type"] == "policy_decision":
                    e["context"]["principal"] = "local"

        self._forge_and_expect(
            doc, row, "the policy saw local, the resolution names agent", context
        )

        # a resolution stripped of its attribution
        def strip(d: dict[str, Any]) -> None:
            for e in resolution(signed)(d):
                e.pop("principal"), e.pop("authentication")

        self._forge_and_expect(doc, row, "a resolution without attribution", strip)

    def test_approval_evidence_messages_and_the_not_applicable_write(self, j: Journal) -> None:
        # a write presenting an artefact for an operation that is not pending: disposition new,
        # verdict approval_not_applicable on a verified presentation, with a decision
        v = {**_post("k1", "c1"), "approval": _artefact(j, "k1")}
        r = j.handle(v)
        assert r.ok and r.disposition == "new"
        doc = _doc(j)
        assert _status(doc, "approval_evidence_is_consistent")[0] == "pass"
        assert _status(doc, "attributions_are_registered")[0] == "pass"  # no approver demanded
        row = "approval_evidence_is_consistent"

        def verdict(d: dict[str, Any], value: str) -> None:
            for e in d["events"]:
                if e["type"] == "policy_decision" and e.get("approval"):
                    e["approval"]["verdict"] = value
                    e["context"]["approval"]["verdict"] = value

        self._forge_and_expect(
            doc,
            row,
            "check result approval_not_applicable cannot reach verdict approval_expired",
            lambda d: verdict(d, "approval_expired"),
        )

        def unverified_valid(d: dict[str, Any]) -> None:
            for e in d["events"]:
                if e["type"] == "approval_presentation":
                    e["verified"] = False
                    e["check_result"] = "checks_passed"
                    e.pop("approval_id", None)
                    e.pop("approver", None)
            verdict(d, "approval_valid")

        # the model itself refuses an unverified presentation with `checks_passed` (expiry and
        # scope are checked only after the signature verified), so the row's clause about it
        # is defence in depth behind the model: unreachable, and its mutants are equivalent
        forged = json.loads(json.dumps(doc))
        unverified_valid(forged)
        with pytest.raises(Exception, match="after the signature verified"):
            load_any(json.dumps(forged))

        def missing_approver(d: dict[str, Any]) -> None:
            verdict(d, "approval_valid")
            for e in d["events"]:
                if e["type"] == "approval_presentation":
                    e["check_result"] = "checks_passed"
                if e["type"] == "policy_decision" and e.get("approval"):
                    e["context"]["approval"]["approver"] = None

        self._forge_and_expect(doc, row, "names its approver", missing_approver)
