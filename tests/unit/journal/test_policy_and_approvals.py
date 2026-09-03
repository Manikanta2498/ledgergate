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
JID, FP = "0" * 32, "a" * 64
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
            journal_id=JID,
            approval_id="a1",
            approver="cfo",
            fingerprint=FP,
            key="k",
            issued_at=EPOCH,
            expires_at=EPOCH + timedelta(hours=1),
        )
        doc = a.to_json()
        assert Approval.from_json(doc) == a
        public = verification_key(verification_key_text(SIGNER))
        ok = check(a, public=public, now=EPOCH, journal_id=JID, fingerprint=FP, key="k")
        assert ok == "checks_passed"
        assert (
            check(
                a,
                public=public,
                now=EPOCH + timedelta(hours=1),
                journal_id=JID,
                fingerprint=FP,
                key="k",
            )
            == "approval_expired"
        )
        assert (
            check(a, public=public, now=EPOCH, journal_id="f" * 32, fingerprint=FP, key="k")
            == "approval_scope_mismatch"
        )
        forged = Approval.from_json({**doc, "approver": "intern"})
        assert (
            check(forged, public=public, now=EPOCH, journal_id=JID, fingerprint=FP, key="k")
            == "approval_invalid"
        )
        wrong_key = verification_key(verification_key_text(generate_signing_key()))
        assert (
            check(a, public=wrong_key, now=EPOCH, journal_id=JID, fingerprint=FP, key="k")
            == "approval_invalid"
        )

    def test_null_display_fields_are_signed_as_null(self) -> None:
        a = issue(
            SIGNER,
            journal_id=JID,
            approval_id="a",
            approver="x",
            fingerprint=FP,
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
            journal_id=JID,
            approval_id="a",
            approver="x",
            fingerprint=FP,
            key="k",
            issued_at=EPOCH,
            expires_at=EPOCH,
        )
        forged = Approval.from_json(
            {**a.to_json(), "key": "other"}
        )  # invalid AND expired AND mis-scoped
        public = verification_key(verification_key_text(SIGNER))
        assert (
            check(forged, public=public, now=EPOCH, journal_id=JID, fingerprint=FP, key="k")
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
        with pytest.raises(ValueError, match="lacks aggregate"):
            POLICY.evaluate(self._ctx("refund", "2999"))  # built for a set without the cap

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
        assert pres[14] == "checks_passed"
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
        assert [p[14] for p in table(gated.path, "approvals")] == [
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
            ({"journal_id": "e" * 32}, "approval_scope_mismatch"),
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
        (out,) = table(gated.path, "outcomes")  # nothing appended: the tip stays
        assert out[3] == "awaiting_approval" and r.outcome == out[0]
        (dec,) = [d for d in table(gated.path, "decisions") if d[6] == "runtime.approval_rejected"]
        assert dec[5] == "deny" and dec[9] == verdict and dec[10] is None
        assert len(table(gated.path, "approval_consumptions")) == 0
        # a plain retry is told what this operation's own request was told
        plain = gated.handle({**open_txn("k1", "t-big", amount=6_000), "call_id": "plain"})
        assert (plain.response, plain.error_type) == ("replayed", "ApprovalRequired")
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
        assert pres[14] == "approval_not_applicable"
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
        assert pres[14] == "approval_not_applicable"

    def test_valid_approval_then_another_rule_denies_is_terminal_and_consumed(
        self, tmp_path: Path
    ) -> None:
        strict = ThresholdPolicySet(
            version="v2",
            approve_above=[Threshold("open_transaction", "USD", 5_000)],
            window_caps=[WindowCap("open_transaction", "USD", 1, timedelta(hours=1))],
        )

        # approve_above fires first only when the cap does not: order is deny, cap, approve.
        # Use a set whose cap is checked after an approval is present via a subclass:
        class CapAfterApproval(ThresholdPolicySet):
            def evaluate(self, context: PolicyContext) -> Decision:
                if context.approval and context.approval.get("verdict") == "approval_valid":
                    return Decision("deny", "v2.other", "some other rule refuses")
                return super().evaluate(context)

        j = Journal.create(
            str(tmp_path / "d.journal"),
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=CapAfterApproval(version=strict.version, approve_above=strict.approve_above),
            approval_key=verification_key_text(SIGNER),
        )
        pending(j, "k1")
        r = present(j, "k1", artefact(j, "k1"))
        assert (r.disposition, r.response, r.error_type) == ("approval", "denied", "PolicyDenied")
        outcomes = table(j.path, "outcomes")
        assert [o[3] for o in outcomes] == ["awaiting_approval", "denied"] and outcomes[1][
            2
        ] == outcomes[0][0]
        assert len(table(j.path, "approval_consumptions")) == 1  # consumed whatever policy said
        again = present(j, "k1", artefact(j, "k1"), call_id="again")
        assert again.response == "replayed" and again.error_type == "PolicyDenied"
        assert table(j.path, "approvals")[-1][14] == "approval_not_applicable"
        j.close()

    def test_artefact_on_a_conflict_is_not_applicable(self, gated: Journal) -> None:
        pending(gated, "k1")
        r = gated.handle(
            {**open_txn("k1", "t-big", amount=7_000), "approval": artefact(gated, "k1")}
        )
        assert r.response == "conflict"
        assert table(gated.path, "approvals")[-1][14] == "approval_not_applicable"
        assert len(table(gated.path, "decisions")) == 1  # conflict writes no decision

    def test_gated_read_with_artefact_records_the_presentation_on_the_decision(
        self, gated: Journal
    ) -> None:
        gated.handle(
            {
                "tool": "trial_balance",
                "call_id": "tb",
                "arguments": {},
                "approval": artefact_free(gated),
            }
        )
        (pres,) = table(gated.path, "approvals")
        (dec,) = table(gated.path, "decisions")
        assert dec[8] == pres[0] and dec[9] == "approval_not_applicable"
        assert json.loads(dec[3])["approval"] == {
            "presentation": pres[0],
            "verdict": "approval_not_applicable",
        }

    def test_approval_under_a_tokenizing_admitter(self, tmp_path: Path) -> None:
        from ledgergate.codec import Tokenizer
        from ledgergate.journal import TokenizingAdmitter

        tk = Tokenizer(bytes(range(32)), domain="acme", key_version="v1")
        j = Journal.create(
            str(tmp_path / "tk.journal"),
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=POLICY,
            admitter=TokenizingAdmitter(tk),
            approval_key=verification_key_text(SIGNER),
        )
        pending(j, "order-42")
        stored_key = tk.tokenize("order-42")
        art = artefact(
            j, stored_key, subject=tk.tokenize("t-big")
        )  # issued against what the journal holds
        r = j.handle(
            {**open_txn("order-42", "t-big", amount=6_000), "call_id": "c2", "approval": art}
        )
        assert (r.disposition, r.response) == ("approval", "applied")
        j.close()

    def test_unbounded_artefact_fields_are_refused_at_admission(self, gated: Journal) -> None:
        pending(gated, "k1")
        bad = artefact(gated, "k1")
        for field, value in (
            ("journal_id", "x" * 200_000),
            ("amount", "1e5"),
            ("currency", "usd"),
            ("signature", "short"),
            ("approver", "two\nlines"),
            ("subject", "x" * 300),
        ):
            r = present(gated, "k1", {**bad, field: value}, call_id=f"c-{field}")
            assert (
                r.response == "invalid" and r.error_message == "approval_malformed at approval"
            ), field
        assert len(table(gated.path, "approvals")) == 0

    def test_display_fields_of_an_unverified_artefact_are_not_stored(self, gated: Journal) -> None:
        pending(gated, "k1")
        forged = {
            **artefact(gated, "k1"),
            "subject": "SECRET FREE TEXT",
        }  # signature no longer covers it
        r = present(gated, "k1", forged)
        assert r.error_message == "runtime.approval_rejected: approval_invalid"
        (pres,) = table(gated.path, "approvals")
        assert (
            pres[14] == "approval_invalid"
            and pres[7] is None
            and pres[8] is None
            and pres[3] is None
            and pres[13] == 0
        )
        everything = " ".join(
            json.dumps(row, default=str) for row in table(gated.path, "approvals")
        )
        assert "SECRET FREE" not in everything

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


class TestConfigurationBinding:
    def test_same_version_label_different_rules_is_refused_at_open(self, tmp_path: Path) -> None:
        path = str(tmp_path / "rules.journal")
        j = Journal.create(
            path, CHART, clock=SteppingClock(EPOCH), ids=SequentialIds(), policy=POLICY
        )
        j.close()
        looser = ThresholdPolicySet(
            version=POLICY.version, approve_above=[Threshold("open_transaction", "USD", 999_999)]
        )
        with pytest.raises(ConfigurationError, match="different rules"):
            Journal.open(path, clock=SteppingClock(EPOCH), ids=SequentialIds(), policy=looser)
        same = Journal.open(path, clock=SteppingClock(EPOCH), ids=SequentialIds(), policy=POLICY)
        same.close()
        assert POLICY.configuration_digest() != looser.configuration_digest()
        assert NullPolicySet().configuration_digest() == "none"

    def test_unverified_presentation_row_holds_no_identity_under_tokenizing_admitter(
        self, tmp_path: Path
    ) -> None:
        from ledgergate.codec import Tokenizer
        from ledgergate.journal import TokenizingAdmitter

        tk = Tokenizer(bytes(range(32)), domain="acme", key_version="v1")
        j = Journal.create(
            str(tmp_path / "t.journal"),
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=POLICY,
            admitter=TokenizingAdmitter(tk),
            approval_key=verification_key_text(SIGNER),
        )
        secrets = {
            "approval_id": "card 4111 1111 1111 1111 exp 12/29",
            "approver": "alice.smith@example.com +1-555-0100",
            "key": "customer SSN 123-45-6789 raw key text",
            "subject": "SECRET SUBJECT",
        }
        forged = {**artefact_free(j), **secrets, "signature": "A" * 86}
        j.handle(
            {**open_txn("k-new", "t-new", amount=10), "approval": forged}
        )  # new: not applicable
        pending(j, "k-pending")
        j.handle(
            {**open_txn("k-pending", "t-big", amount=6_000), "call_id": "c2", "approval": forged}
        )  # approval: invalid
        stored = " ".join(json.dumps(r, default=str) for r in table(j.path, "approvals"))
        for value in secrets.values():
            assert value not in stored, value
        assert [r[14] for r in table(j.path, "approvals")] == [
            "approval_not_applicable",
            "approval_invalid",
        ]
        assert all(
            r[13] == 0 and r[3] is None and r[4] is None and r[6] is None
            for r in table(j.path, "approvals")
        )
        j.close()

    def test_verified_not_applicable_presentation_keeps_identity(self, gated: Journal) -> None:
        gated.handle({**open_txn("k9", "t-new", amount=10), "approval": artefact_free(gated)})
        (pres,) = table(gated.path, "approvals")
        assert pres[13] == 1 and pres[3] == "free" and pres[14] == "approval_not_applicable"


class TestSignedTimestampsAndValidation:
    def test_any_rendering_of_the_same_instant_verifies(self) -> None:
        from datetime import timezone

        a = issue(
            SIGNER,
            journal_id=JID,
            approval_id="a",
            approver="x",
            fingerprint=FP,
            key="k",
            issued_at=EPOCH,
            expires_at=EPOCH + timedelta(hours=1),
        )
        doc = a.to_json()
        plus_one = (EPOCH + timedelta(hours=1)).astimezone(timezone(timedelta(hours=1))).isoformat()
        zulu = doc["issued_at"].replace("+00:00", "Z")
        represented = Approval.from_json({**doc, "expires_at": plus_one, "issued_at": zulu})
        public = verification_key(verification_key_text(SIGNER))
        assert (
            check(represented, public=public, now=EPOCH, journal_id=JID, fingerprint=FP, key="k")
            == "checks_passed"
        )
        assert doc["issued_at"].endswith("+00:00")

    def test_rules_are_validated_at_construction(self) -> None:
        with pytest.raises(ValueError, match="whole number of seconds"):
            ThresholdPolicySet(
                version="v", window_caps=[WindowCap("refund", "USD", 1, timedelta(seconds=1.5))]
            )
        with pytest.raises(ValueError, match="non-negative int"):
            ThresholdPolicySet(version="v", deny_above=[Threshold("refund", "USD", 10.0)])  # type: ignore[arg-type]

    def test_a_raising_policy_set_is_a_configuration_error_and_unrecorded(
        self, tmp_path: Path
    ) -> None:
        class Broken(NullPolicySet):
            version = "broken"

            def evaluate(self, context: PolicyContext) -> Decision:
                raise RuntimeError("boom")

        j = Journal.create(
            str(tmp_path / "b.journal"),
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=Broken(),
        )
        before = len(table(j.path, "journal"))
        with pytest.raises(ConfigurationError, match="raised"):
            j.handle(open_txn("k", "t", amount=1))
        assert len(table(j.path, "journal")) == before
        j.close()


class TestWindowTimeBase:
    def test_an_approved_operation_counts_at_approval_time(self, tmp_path: Path) -> None:
        """The window's time base is the producing invocation's requested_at: for an approved
        operation that is the approval, not the original request."""
        policy = ThresholdPolicySet(
            version="w1",
            approve_above=[Threshold("refund", "USD", 1_000)],
            window_caps=[WindowCap("refund", "USD", 3_000, timedelta(hours=1))],
        )
        clock = SteppingClock(EPOCH, step=timedelta(minutes=1))
        j = Journal.create(
            str(tmp_path / "w.journal"),
            CHART,
            clock=clock,
            ids=SequentialIds(),
            policy=policy,
            approval_key=verification_key_text(SIGNER),
        )
        assert j.handle(open_txn("o", "t", amount=10_000)).response == "applied"
        assert j.handle(_advance("a", "authorize")).response == "applied"
        assert (
            j.handle(_advance("s", "settle", _entry(10_000, "cash", "revenue"))).response
            == "applied"
        )
        big = _refund("r-big", 2_500)  # above the approval line
        assert j.handle(big).response == "awaiting_approval"
        # two hours pass before the approval lands
        clock2 = SteppingClock(EPOCH + timedelta(hours=2), step=timedelta(minutes=1))
        j.close()
        j = Journal.open(
            str(tmp_path / "w.journal"), clock=clock2, ids=SequentialIds(start=50), policy=policy
        )
        art = artefact(
            j,
            "r-big",
            subject="t",
            amount="2500",
            issued_at=EPOCH + timedelta(hours=2),
            expires_at=EPOCH + timedelta(hours=3),
        )
        assert j.handle({**big, "call_id": "c-appr", "approval": art}).response == "applied"
        # a refund a few minutes after the approval sees 2_500 in the window, though the
        # original request was two hours ago
        r = j.handle(_refund("r-next", 800))
        assert r.response == "denied" and "window_cap" in (r.error_message or "")
        (dec,) = [d for d in table(j.path, "decisions") if d[6] == "w1.window_cap"]
        assert json.loads(dec[3])["aggregates"] == {"applied.refund.USD.3600s": "2500"}
        j.close()


class TestNoPolicyCodeOnFailedVerdict:
    def test_subject_and_aggregates_are_not_computed_and_a_raising_set_cannot_lose_the_presentation(
        self, tmp_path: Path
    ) -> None:
        calls: list[str] = []

        class Spy(ThresholdPolicySet):
            def subject_of(self, command: Any) -> str | None:
                calls.append("subject_of")
                return super().subject_of(command)

            def aggregates_for(self, command: Any, now: Any, history: Any) -> dict[str, Any]:
                calls.append("aggregates_for")
                if calls.count("aggregates_for") > 1:
                    raise RuntimeError("would have lost the presentation")
                return super().aggregates_for(command, now, history)

            def evaluate(self, context: PolicyContext) -> Decision:
                calls.append("evaluate")
                return super().evaluate(context)

        j = Journal.create(
            str(tmp_path / "spy.journal"),
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=Spy(version=POLICY.version, approve_above=POLICY.approve_above),
            approval_key=verification_key_text(SIGNER),
        )
        pending(j, "k1")
        calls.clear()
        r = present(j, "k1", artefact(j, "k1", expires_at=EPOCH + timedelta(seconds=1)))
        assert r.error_type == "ApprovalRejected" and calls == []
        (dec,) = [d for d in table(j.path, "decisions") if d[6] == "runtime.approval_rejected"]
        ctx = json.loads(dec[3])
        assert ctx["subject"] is None and ctx["aggregates"] == {}
        assert len(table(j.path, "approvals")) == 1  # the presentation is kept
        j.close()

    def test_schema_ties_consumption_to_a_valid_verdict(self, gated: Journal) -> None:
        pending(gated, "k1")
        present(gated, "k1", artefact(gated, "k1"))
        conn = sqlite3.connect(gated.path, isolation_level=None)
        try:
            (pres,) = [r for r in rows(conn, "approvals") if r[14] == "checks_passed"]
            conn.execute("BEGIN")
            seq = conn.execute(
                "INSERT INTO journal (kind) VALUES ('approval_consumptions')"
            ).lastrowid
            with pytest.raises(sqlite3.IntegrityError):  # UNIQUE on approval_id, or the trigger
                conn.execute(
                    "INSERT INTO approval_consumptions VALUES (?,?,?,?)",
                    (seq, "other-id", pres[0] + 1000, pres[1]),
                )
            conn.execute("ROLLBACK")
        finally:
            conn.close()
