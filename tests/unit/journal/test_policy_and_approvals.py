"""M3: the policy layer and the approval protocol, held to docs/spec/journal.md
(*Approval artefacts*, the two decision-to-outcome tables) and the ThresholdPolicySet rules."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tests.unit.journal.support import CHART, open_txn, rows

from ledgergate.journal import (
    Approval,
    ApprovalError,
    ConfigurationError,
    Journal,
    PolicyContext,
    Threshold,
    ThresholdPolicySet,
    WindowCap,
    check,
    generate_signing_key,
    issue,
    verification_key,
    verification_key_text,
)
from ledgergate.journal.policy import Decision, NullPolicySet
from ledgergate.ledger import EPOCH, SequentialIds, SteppingClock

SIGNER = generate_signing_key()
POLICY = ThresholdPolicySet(
    version="refunds-v1",
    deny_above=[Threshold("open_transaction", "USD", 100_000)],
    approve_above=[Threshold("open_transaction", "USD", 5_000)],
    window_caps=[WindowCap("refund", "USD", 3_000, timedelta(hours=24))],
    gated_reads=frozenset({"trial_balance"}),
)


@pytest.fixture
def gated(tmp_path: Path) -> Iterator[Journal]:
    j = Journal.create(
        str(tmp_path / "g.journal"),
        CHART,
        clock=SteppingClock(EPOCH),
        ids=SequentialIds(),
        policy=POLICY,
        approval_key=verification_key_text(SIGNER),
    )
    yield j
    j.close()


def table(path: str, name: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(path)
    try:
        return rows(conn, name)
    finally:
        conn.close()


def artefact(
    j: Journal, key: str, *, signer: Ed25519PrivateKey = SIGNER, **over: Any
) -> dict[str, Any]:
    (op,) = [r for r in table(j.path, "operations") if r[1] == key]
    fields: dict[str, Any] = {
        "journal_id": j.definition.journal_id,
        "approval_id": f"appr-{key}",
        "approver": "cfo",
        "fingerprint": op[2],
        "key": key,
        "issued_at": EPOCH,
        "expires_at": EPOCH + timedelta(days=1),
        "subject": "t-big",
        "amount": "6000",
        "currency": "USD",
    }
    fields.update(over)
    return issue(signer, **fields).to_json()


def pending(j: Journal, key: str = "k1", amount: int = 6_000) -> None:
    r = j.handle(open_txn(key, "t-big", amount=amount))
    assert r.response == "awaiting_approval", r


def present(
    j: Journal, key: str, approval: dict[str, Any], call_id: str = "c-approve", amount: int = 6_000
) -> Any:
    return j.handle(
        {**open_txn(key, "t-big", amount=amount), "call_id": call_id, "approval": approval}
    )


class TestArtefacts:
    def test_issue_verify_and_round_trip(self) -> None:
        a = issue(
            SIGNER,
            journal_id="j",
            approval_id="a1",
            approver="cfo",
            fingerprint="f",
            key="k",
            issued_at=EPOCH,
            expires_at=EPOCH + timedelta(hours=1),
        )
        doc = a.to_json()
        assert Approval.from_json(doc) == a
        public = verification_key(verification_key_text(SIGNER))
        ok = check(a, public=public, now=EPOCH, journal_id="j", fingerprint="f", key="k")
        assert ok == "checks_passed"
        assert (
            check(
                a,
                public=public,
                now=EPOCH + timedelta(hours=1),
                journal_id="j",
                fingerprint="f",
                key="k",
            )
            == "approval_expired"
        )
        assert (
            check(a, public=public, now=EPOCH, journal_id="other", fingerprint="f", key="k")
            == "approval_scope_mismatch"
        )
        forged = Approval.from_json({**doc, "approver": "intern"})
        assert (
            check(forged, public=public, now=EPOCH, journal_id="j", fingerprint="f", key="k")
            == "approval_invalid"
        )
        wrong_key = verification_key(verification_key_text(generate_signing_key()))
        assert (
            check(a, public=wrong_key, now=EPOCH, journal_id="j", fingerprint="f", key="k")
            == "approval_invalid"
        )

    def test_null_display_fields_are_signed_as_null(self) -> None:
        a = issue(
            SIGNER,
            journal_id="j",
            approval_id="a",
            approver="x",
            fingerprint="f",
            key="k",
            issued_at=EPOCH,
            expires_at=EPOCH + timedelta(hours=1),
        )
        assert a.subject is None and '"subject":null' in json.dumps(
            a.signed_payload(), separators=(",", ":")
        )
        with pytest.raises(ApprovalError, match="string or null"):
            Approval.from_json({**a.to_json(), "amount": 5})

    @pytest.mark.parametrize("bad", [{"x": 1}, "s", {"journal_id": "j"}, None])
    def test_malformed_shapes(self, bad: object) -> None:
        with pytest.raises(ApprovalError):
            Approval.from_json(bad)

    def test_checks_short_circuit_in_order(self) -> None:
        a = issue(
            SIGNER,
            journal_id="j",
            approval_id="a",
            approver="x",
            fingerprint="f",
            key="k",
            issued_at=EPOCH,
            expires_at=EPOCH,
        )
        forged = Approval.from_json(
            {**a.to_json(), "key": "other"}
        )  # invalid AND expired AND mis-scoped
        public = verification_key(verification_key_text(SIGNER))
        assert (
            check(forged, public=public, now=EPOCH, journal_id="j", fingerprint="f", key="k")
            == "approval_invalid"
        )


class TestThresholdPolicySet:
    def _ctx(
        self,
        kind: str,
        amount: str | None,
        approval: dict[str, Any] | None = None,
        agg: dict[str, str] | None = None,
    ) -> PolicyContext:
        return PolicyContext(
            "local",
            "t",
            "0" * 64,
            "fingerprint",
            EPOCH,
            POLICY.version,
            kind,
            amount,
            None if amount is None else "USD",
            dict(agg or {}),
            approval,
        )

    def test_rule_order(self) -> None:
        assert POLICY.evaluate(self._ctx("open_transaction", "100001")).decision == "deny"
        assert (
            POLICY.evaluate(self._ctx("open_transaction", "6000")).decision == "approval_required"
        )
        assert (
            POLICY.evaluate(
                self._ctx("open_transaction", "6000", {"verdict": "approval_valid"})
            ).decision
            == "allow"
        )
        assert (
            POLICY.evaluate(
                self._ctx("open_transaction", "6000", {"verdict": "approval_not_applicable"})
            ).decision
            == "approval_required"
        )
        assert POLICY.evaluate(self._ctx("open_transaction", "5000")).decision == "allow"
        capped = self._ctx("refund", "1500", agg={"applied.refund.USD.86400s": "2000"})
        assert POLICY.evaluate(capped).matched_rule == "refunds-v1.window_cap"
        assert POLICY.evaluate(self._ctx("post", None)).matched_rule == "refunds-v1.no_amount"
        assert POLICY.evaluate(self._ctx("refund", "2999")).decision == "allow"  # under the cap

    def test_version_none_is_reserved(self) -> None:
        with pytest.raises(ValueError, match="null policy set"):
            ThresholdPolicySet(version="none")
        assert NullPolicySet().evaluate(self._ctx("open_transaction", "999999")) == Decision(
            "allow", "none.allow_all", "null policy set: no rules configured"
        )


class TestApprovalProtocol:
    def test_two_step_approval_applies_once_and_consumes(self, gated: Journal) -> None:
        pending(gated, "k1")
        (out,) = table(gated.path, "outcomes")
        assert out[3] == "awaiting_approval"
        r = present(gated, "k1", artefact(gated, "k1"))
        assert (r.disposition, r.response, r.ok) == ("approval", "applied", True)
        outcomes = table(gated.path, "outcomes")
        assert [o[3] for o in outcomes] == ["awaiting_approval", "applied"]
        assert outcomes[1][2] == outcomes[0][0]  # chained to the pending root
        (pres,) = table(gated.path, "approvals")
        assert pres[13] == "checks_passed"
        (cons,) = table(gated.path, "approval_consumptions")
        assert cons[1] == "appr-k1" and cons[2] == pres[0]
        decisions = table(gated.path, "decisions")
        assert decisions[0][5] == "approval_required" and decisions[1][5] == "allow"
        assert (
            decisions[1][8] == pres[0]
            and decisions[1][9] == "approval_valid"
            and decisions[1][10] == cons[0]
        )
        ctx = json.loads(decisions[1][3])
        assert ctx["approval"] == {"presentation": pres[0], "verdict": "approval_valid"}
        assert (
            ctx["subject"] == "t-big"
            and ctx["amount"] == "6000"
            and ctx["command_kind"] == "open_transaction"
        )
        # replay after approval says applied; a re-presented artefact is not applicable
        again = present(gated, "k1", artefact(gated, "k1"), call_id="again")
        assert (again.disposition, again.response) == ("replay", "replayed") and again.ok
        assert [p[13] for p in table(gated.path, "approvals")] == [
            "checks_passed",
            "approval_not_applicable",
        ]
        assert len(table(gated.path, "approval_consumptions")) == 1

    def test_retry_without_artefact_replays_awaiting(self, gated: Journal) -> None:
        pending(gated, "k1")
        r = gated.handle({**open_txn("k1", "t-big", amount=6_000), "call_id": "retry"})
        assert (r.disposition, r.response, r.error_type) == (
            "replay",
            "replayed",
            "ApprovalRequired",
        )

    @pytest.mark.parametrize(
        ("over", "verdict"),
        [
            ({"expires_at": EPOCH + timedelta(seconds=1)}, "approval_expired"),
            ({"journal_id": "someone-elses"}, "approval_scope_mismatch"),
            ({"fingerprint": "0" * 64}, "approval_scope_mismatch"),
            ({"signer": generate_signing_key()}, "approval_invalid"),
        ],
    )
    def test_failed_verdicts_deny_by_the_runtime_and_keep_the_operation_pending(
        self, gated: Journal, over: dict[str, Any], verdict: str
    ) -> None:
        pending(gated, "k1")
        r = present(gated, "k1", artefact(gated, "k1", **over))
        assert (r.disposition, r.response, r.error_type) == (
            "approval",
            "awaiting_approval",
            "ApprovalRejected",
        )
        assert r.error_message == f"runtime.approval_rejected: {verdict}"
        outcomes = table(gated.path, "outcomes")
        assert [o[3] for o in outcomes] == ["awaiting_approval", "awaiting_approval"]
        assert outcomes[1][2] == outcomes[0][0]
        (dec,) = [d for d in table(gated.path, "decisions") if d[6] == "runtime.approval_rejected"]
        assert dec[5] == "deny" and dec[9] == verdict and dec[10] is None
        assert len(table(gated.path, "approval_consumptions")) == 0
        # still pending: a good artefact now succeeds
        ok = present(gated, "k1", artefact(gated, "k1", approval_id="appr-second"), call_id="c2")
        assert ok.response == "applied"

    def test_a_distinct_artefact_reusing_an_approval_id_is_already_used(
        self, gated: Journal
    ) -> None:
        pending(gated, "k1")
        pending(gated, "k2")
        present(gated, "k1", artefact(gated, "k1", approval_id="shared"))
        r = present(gated, "k2", artefact(gated, "k2", approval_id="shared"), call_id="c2")
        assert (
            r.response == "awaiting_approval"
            and r.error_message == "runtime.approval_rejected: approval_already_used"
        )
        assert len(table(gated.path, "approval_consumptions")) == 1

    def test_artefact_on_a_new_operation_is_not_applicable_and_policy_still_runs(
        self, gated: Journal
    ) -> None:
        r = gated.handle({**open_txn("k9", "t-new", amount=10), "approval": artefact_free(gated)})
        assert r.response == "applied"
        (pres,) = table(gated.path, "approvals")
        assert pres[13] == "approval_not_applicable"
        (dec,) = table(gated.path, "decisions")
        assert (
            dec[9] == "approval_not_applicable"
            and dec[8] == pres[0]
            and json.loads(dec[3])["approval"]["verdict"] == "approval_not_applicable"
        )

    def test_artefact_on_a_read_is_recorded_not_applicable(self, gated: Journal) -> None:
        gated.handle(
            {
                "tool": "balance",
                "call_id": "r",
                "arguments": {"account": "cash"},
                "approval": artefact_free(gated),
            }
        )
        (pres,) = table(gated.path, "approvals")
        assert pres[13] == "approval_not_applicable"

    def test_deny_and_gated_read(self, gated: Journal) -> None:
        r = gated.handle(open_txn("big", "t-huge", amount=200_000))
        assert r.response == "denied" and "deny_above" in (r.error_message or "")
        rd = gated.handle({"tool": "trial_balance", "call_id": "tb", "arguments": {}})
        assert rd.ok  # gated but allowed: the set has no read rules, so allow
        (dec,) = [d for d in table(gated.path, "decisions") if d[2] is None]
        assert json.loads(dec[3])["digest_kind"] == "request"

    def test_window_cap_reads_history_and_records_the_aggregate(self, gated: Journal) -> None:
        """Refunds against one transaction accumulate; the cap fires on the split that would
        cross it, even though the core alone would have allowed it."""
        assert gated.handle(open_txn("o", "t", amount=4_000)).response == "applied"
        assert gated.handle(_advance("a", "authorize")).response == "applied"
        assert (
            gated.handle(_advance("s", "settle", _entry(4_000, "cash", "revenue"))).response
            == "applied"
        )
        first = gated.handle(_refund("r1", 2_000))
        assert first.response == "applied"
        second = gated.handle(_refund("r2", 1_500))  # 3_500 > 3_000 within 24h; core would allow
        assert second.response == "denied" and "window_cap" in (second.error_message or "")
        (dec,) = [d for d in table(gated.path, "decisions") if d[6] == "refunds-v1.window_cap"]
        assert json.loads(dec[3])["aggregates"] == {"applied.refund.USD.86400s": "2000"}
        # outside the window the total is not counted
        from ledgergate.journal.store import _History

        hist = _History(gated)
        assert hist.applied_total(subject="t", kind="refund", currency="USD", since=EPOCH) == 2_000
        assert (
            hist.applied_total(
                subject="t", kind="refund", currency="USD", since=EPOCH + timedelta(days=2)
            )
            == 0
        )

    def test_policy_asking_for_approval_after_a_valid_one_is_fatal_and_unrecorded(
        self, tmp_path: Path
    ) -> None:
        class Insatiable(ThresholdPolicySet):
            def evaluate(self, context: PolicyContext) -> Decision:
                return Decision("approval_required", "bad.always", "asks again")

        bad = Insatiable(version="bad-v1")
        j = Journal.create(
            str(tmp_path / "bad.journal"),
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=bad,
            approval_key=verification_key_text(SIGNER),
        )
        pending(j, "k1", amount=10)
        before = len(table(j.path, "journal"))
        with pytest.raises(ConfigurationError, match="misconfigured"):
            present(j, "k1", artefact(j, "k1"), amount=10)
        assert len(table(j.path, "journal")) == before  # rolled back, nothing consumed
        assert len(table(j.path, "approval_consumptions")) == 0
        j.close()

    def test_journal_without_verification_key_never_verifies(self, tmp_path: Path) -> None:
        j = Journal.create(
            str(tmp_path / "nokey.journal"),
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=POLICY,
        )
        pending(j, "k1")
        r = present(j, "k1", artefact(j, "k1"))
        assert r.error_message == "runtime.approval_rejected: approval_invalid"
        j.close()
        with pytest.raises(ValueError):
            Journal.create(
                str(tmp_path / "badkey.journal"),
                CHART,
                clock=SteppingClock(EPOCH),
                ids=SequentialIds(),
                approval_key="not-a-key",
            )


def _advance(key: str, event: str, entry: dict[str, Any] | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"transaction_id": "t", "event": event}
    if entry is not None:
        args["entry"] = entry
    return {"tool": "advance", "call_id": f"c-{key}", "key": key, "arguments": args}


def _entry(amount: int, debit: str, credit: str) -> dict[str, Any]:
    return {
        "postings": [
            {"account": debit, "side": "debit", "money": {"amount": amount, "currency": "USD"}},
            {"account": credit, "side": "credit", "money": {"amount": amount, "currency": "USD"}},
        ]
    }


def _refund(key: str, amount: int) -> dict[str, Any]:
    return {
        "tool": "refund",
        "call_id": f"c-{key}",
        "key": key,
        "arguments": {
            "transaction_id": "t",
            "money": {"amount": amount, "currency": "USD"},
            "entry": _entry(amount, "revenue", "cash"),
        },
    }


def artefact_free(j: Journal) -> dict[str, Any]:
    return issue(
        SIGNER,
        journal_id=j.definition.journal_id,
        approval_id="free",
        approver="cfo",
        fingerprint="0" * 64,
        key="whatever",
        issued_at=EPOCH,
        expires_at=EPOCH + timedelta(days=1),
    ).to_json()
