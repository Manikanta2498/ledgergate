"""`approval_evidence_is_consistent` (docs/spec/trace-v2.md, *Approval evidence*;
docs/spec/journal.md, approval protocol checks 1..4): the verdict a decision recorded has to
be one its own presentation's check result can reach, `approval_valid` needs a verified
presentation whose checks passed and a logical approval id no other valid decision used, and
a verified presentation on an applicable verdict names its approver in the context.

Every case here starts from a journal-derived trace that verifies, forges exactly one field,
and requires the row to fail: each was a PASS before this row existed.
"""

from __future__ import annotations

import copy
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from ledgergate.derive import trace as derive
from ledgergate.invariants import check
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
    SequentialIds,
    SteppingClock,
)
from ledgergate.trace import TraceV2

CHART = ChartOfAccounts(
    [Account("cash", AccountType.ASSET, USD), Account("revenue", AccountType.REVENUE, USD)]
)
SIGNER = generate_signing_key()
POLICY = ThresholdPolicySet(version="p", approve_above=[Threshold("open_transaction", "USD", 100)])


def _open(key: str, call_id: str, amount: int, subject: str = "t", **extra: Any) -> dict[str, Any]:
    return {
        "tool": "open_transaction",
        "call_id": call_id,
        "key": key,
        "arguments": {
            "transaction_id": subject,
            "amount": {"amount": amount, "currency": "USD"},
        },
        **extra,
    }


def _artefact(j: Journal, key: str, approval_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(j.path)
    try:
        (fp,) = conn.execute("SELECT fingerprint FROM operations WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return issue(
        SIGNER,
        journal_id=j.definition.journal_id,
        approval_id=approval_id,
        approver="cfo",
        fingerprint=fp,
        key=key,
        issued_at=EPOCH,
        expires_at=EPOCH + timedelta(days=1),
    ).to_json()


def _journal(tmp_path: Path, *, second: bool = False) -> str:
    """One approved operation, and optionally a second with its own logical approval id."""
    path = str(tmp_path / "a.journal")
    j = Journal.create(
        path,
        CHART,
        clock=SteppingClock(EPOCH),
        ids=SequentialIds(),
        policy=POLICY,
        approvers={"cfo": verification_key_text(SIGNER)},
    )
    j.handle(_open("k1", "c1", 500))
    j.handle(_open("k1", "c2", 500, approval=_artefact(j, "k1", "one")))
    if second:
        j.handle(_open("k2", "c3", 700, "u"))
        j.handle(_open("k2", "c4", 700, "u", approval=_artefact(j, "k2", "two")))
    j.close()
    return path


def _statuses(doc: dict[str, Any]) -> dict[str, str]:
    return {r.name: r.status for r in check(TraceV2.model_validate(doc)).results}


@pytest.fixture
def approved(tmp_path: Path) -> dict[str, Any]:
    t = derive(_journal(tmp_path))
    assert check(t).passed
    return t.model_dump(mode="json", exclude_none=True)


class TestCheckResultToVerdict:
    def test_an_unverified_invalid_presentation_cannot_have_a_valid_verdict(
        self, approved: dict[str, Any]
    ) -> None:
        doc = copy.deepcopy(approved)
        p = next(e for e in doc["events"] if e["type"] == "approval_presentation")
        p["verified"], p["check_result"] = False, "approval_invalid"
        p.pop("approval_id")
        p.pop("approver")
        d = next(e for e in doc["events"] if e["type"] == "policy_decision" and e.get("approval"))
        d["context"]["approval"].pop("approver")
        assert d["approval"]["verdict"] == "approval_valid" and d["consumption_ref"]
        assert _statuses(doc)["approval_evidence_is_consistent"] == "fail"

    def test_an_expired_presentation_cannot_have_a_valid_verdict(
        self, approved: dict[str, Any]
    ) -> None:
        doc = copy.deepcopy(approved)
        p = next(e for e in doc["events"] if e["type"] == "approval_presentation")
        p["check_result"] = "approval_expired"
        assert _statuses(doc)["approval_evidence_is_consistent"] == "fail"

    def test_a_passing_check_cannot_have_a_failing_verdict(self, approved: dict[str, Any]) -> None:
        doc = copy.deepcopy(approved)
        d = next(e for e in doc["events"] if e["type"] == "policy_decision" and e.get("approval"))
        d["approval"]["verdict"] = "approval_scope_mismatch"
        d["context"]["approval"]["verdict"] = "approval_scope_mismatch"
        d.pop("consumption_ref", None)
        assert _statuses(doc)["approval_evidence_is_consistent"] == "fail"


class TestLogicalApprovalIdIsSpentOnce:
    def test_two_valid_decisions_on_one_logical_approval_id_fail(self, tmp_path: Path) -> None:
        t = derive(_journal(tmp_path, second=True))
        assert check(t).passed
        doc = t.model_dump(mode="json", exclude_none=True)
        presentations = [e for e in doc["events"] if e["type"] == "approval_presentation"]
        assert len(presentations) == 2
        # distinct consumption rows, one logical approval: what the consumptions table's
        # UNIQUE on the logical id forbids, and what checking the rows alone missed
        presentations[1]["approval_id"] = presentations[0]["approval_id"]
        assert _statuses(doc)["approval_evidence_is_consistent"] == "fail"
        row = next(
            r
            for r in check(TraceV2.model_validate(doc)).results
            if r.name == "approval_evidence_is_consistent"
        )
        assert any("was already consumed by" in f.message for f in row.findings)


class TestVerifiedPresentationsNameTheirApprover:
    def test_a_stripped_approver_is_caught_without_any_registry_event(
        self, approved: dict[str, Any]
    ) -> None:
        """A pre-schema-7-shaped forgery: no registry events and no attributions, so
        `attributions_are_registered` has no evidence and `decision_recomputes` cannot run
        check 1b. The approval-evidence row still judges it."""
        doc = copy.deepcopy(approved)
        doc["events"] = [
            e for e in doc["events"] if e["type"] not in ("principal_change", "approver_change")
        ]
        for e in doc["events"]:
            if e["type"] == "invocation_resolution":
                e.pop("principal", None)
                e.pop("authentication", None)
            if e["type"] == "policy_decision" and e["context"].get("approval"):
                e["context"]["approval"].pop("approver", None)
        for i, e in enumerate(doc["events"]):
            e["seq"] = i + 1
        statuses = _statuses(doc)
        assert statuses["attributions_are_registered"] == "no_evidence"
        assert statuses["approval_evidence_is_consistent"] == "fail"

    def test_a_derived_trace_passes_the_row(self, approved: dict[str, Any]) -> None:
        assert _statuses(approved)["approval_evidence_is_consistent"] == "pass"


class TestReachableBranchesTheFirstPassCalledEquivalent:
    """The second review showed these branches reachable; each test asserts the finding text
    of the branch, so the mutants that blank or reword it, and the operator mutants inside its
    condition, die here rather than being recorded as equivalent."""

    def _row(self, doc: dict[str, Any]) -> list[str]:
        card = check(TraceV2.model_validate(doc))
        return [
            f.message
            for r in card.results
            if r.name == "approval_evidence_is_consistent"
            for f in r.findings
        ]

    def test_a_verdict_against_a_missing_presentation_is_named_and_every_intent_is_judged(
        self, approved: dict[str, Any]
    ) -> None:
        doc = copy.deepcopy(approved)
        for e in doc["events"]:
            if e["type"] == "policy_decision" and e.get("approval"):
                e["approval"]["presentation_ref"] = "presentation-999"
                e["context"]["approval"]["presentation"] = 999
        messages = self._row(doc)
        assert messages == [
            "intent-11: verdict against presentation-999, which carries no presentation"
        ]

    def test_two_missing_presentations_yield_two_findings(self, tmp_path: Path) -> None:
        # `continue` -> `break` after the first missing presentation would report one
        doc = derive(_journal(tmp_path, second=True)).model_dump(mode="json", exclude_none=True)
        for e in doc["events"]:
            if e["type"] == "policy_decision" and e.get("approval"):
                e["approval"]["presentation_ref"] = "presentation-999"
                e["context"]["approval"]["presentation"] = 999
        messages = self._row(doc)
        assert len([m for m in messages if "carries no presentation" in m]) == 2

    def test_an_expired_presentation_with_a_valid_verdict_trips_both_branches(
        self, approved: dict[str, Any]
    ) -> None:
        doc = copy.deepcopy(approved)
        p = next(e for e in doc["events"] if e["type"] == "approval_presentation")
        p["check_result"] = "approval_expired"
        assert self._row(doc) == [
            "intent-11: check result approval_expired cannot reach verdict approval_valid",
            "intent-11: approval_valid on a presentation that is not verified with checks_passed",
        ]
