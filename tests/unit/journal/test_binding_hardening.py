"""Whole-project review: definition-bound components could change after open, rules could be
mutated behind the digest, an unkeyed request digest committed to unverified approval fields,
deep valid JSON escaped the recorded-invalid guarantee, threshold rules over commands without
an amount were silently ignored, and journal admission accepted inputs the trace could not
carry."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from ledgergate.codec import Tokenizer, digest
from ledgergate.journal import (
    ConfigurationError,
    IdentityAdmitter,
    Journal,
    JournalError,
    NullPolicySet,
    Threshold,
    ThresholdPolicySet,
    TokenizingAdmitter,
    WindowCap,
    generate_signing_key,
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

CHART = ChartOfAccounts(
    [Account("cash", AccountType.ASSET, USD), Account("revenue", AccountType.REVENUE, USD)]
)
SALE = {
    "postings": [
        {"account": "cash", "side": "debit", "money": {"amount": 5, "currency": "USD"}},
        {"account": "revenue", "side": "credit", "money": {"amount": 5, "currency": "USD"}},
    ]
}


def _open(key: str, amount: int) -> dict[str, Any]:
    return {
        "tool": "open_transaction",
        "call_id": f"c-{key}",
        "key": key,
        "arguments": {"transaction_id": key, "amount": {"amount": amount, "currency": "USD"}},
    }


def _journal(tmp_path: Path, **kw: Any) -> Journal:
    return Journal.create(
        str(tmp_path / "j.journal"), CHART, clock=SteppingClock(EPOCH), ids=SequentialIds(), **kw
    )


class TestBoundComponents:
    def test_policy_and_admitter_cannot_be_replaced_after_open(self, tmp_path: Path) -> None:
        strict = ThresholdPolicySet(
            version="s", deny_above=[Threshold("open_transaction", "USD", 100)]
        )
        j = _journal(tmp_path, policy=strict)
        assert j.handle(_open("k1", 500)).response == "denied"
        with pytest.raises(ConfigurationError, match="bound at open"):
            j.policy = NullPolicySet()
        with pytest.raises(ConfigurationError, match="bound at open"):
            j.admitter = IdentityAdmitter()
        assert j.handle(_open("k2", 500)).response == "denied"
        j.close()

    def test_rules_mutated_behind_the_digest_are_caught_at_the_next_transaction(
        self, tmp_path: Path
    ) -> None:
        rules = [Threshold("open_transaction", "USD", 100)]
        strict = ThresholdPolicySet(version="s", deny_above=rules)
        j = _journal(tmp_path, policy=strict)
        rules.clear()  # the caller's list; the set copied it
        assert j.handle(_open("k1", 500)).response == "denied"
        # even a forced mutation of the frozen set is caught before the next write
        object.__setattr__(strict, "deny_above", ())
        with pytest.raises(ConfigurationError, match="no longer matches"):
            j.handle(_open("k2", 500))
        j.close()

    def test_definition_stores_the_declarative_configuration(self, tmp_path: Path) -> None:
        strict = ThresholdPolicySet(
            version="s",
            deny_above=[Threshold("open_transaction", "USD", 100)],
            window_caps=[WindowCap("refund", "USD", 50, timedelta(hours=1))],
            gated_reads=frozenset({"balance"}),
        )
        j = _journal(tmp_path, policy=strict)
        text = j.definition.policy_configuration
        assert text is not None
        doc = json.loads(text)
        assert ThresholdPolicySet.from_configuration(doc) == strict
        assert digest(doc) == j.definition.policy_config
        j.close()
        (tmp_path / "n").mkdir()
        null = _journal(tmp_path / "n", policy=NullPolicySet())
        assert null.definition.policy_configuration is not None
        null.close()


class TestRequestDigestAndLimits:
    def test_request_digest_excludes_the_unverified_artefact(self, tmp_path: Path) -> None:
        from ledgergate.journal.admission import AdmissionScope, Request

        base: dict[str, Any] = {
            "tool": "open_transaction",
            "arguments": {},
            "call_id": "c",
            "principal": "local",
            "key": "k",
        }
        plain = Request(**base).request_digest()
        with_art = Request(**base, approval={"approver": "alice"}).request_digest()
        assert plain == with_art
        del AdmissionScope

    def test_deep_valid_json_is_refused_as_transport_not_tracebacked(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        deep: Any = {}
        cur = deep
        for _ in range(1_200):
            cur["x"] = {}
            cur = cur["x"]
        with pytest.raises(JournalError, match="nesting"):
            j.handle(
                {
                    "tool": "post",
                    "call_id": "c",
                    "key": "k",
                    "arguments": {"draft": SALE, "extra": deep},
                }
            )
        conn = sqlite3.connect(j.path)
        assert conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0] == 0
        conn.close()
        j.close()

    def test_arguments_beyond_the_trace_payload_bound_are_recorded_invalid(
        self, tmp_path: Path
    ) -> None:
        j = _journal(tmp_path)
        wide = {"draft": SALE, "noise": [0] * 10_001}
        r = j.handle({"tool": "post", "call_id": "c", "key": "k", "arguments": wide})
        assert r.response == "invalid" and "payload_too_large" in (r.error_message or "")
        j.close()

    def test_too_many_postings_and_too_long_messages_are_refused(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        postings = [SALE["postings"][0]] * 1001 + [
            {"account": "revenue", "side": "credit", "money": {"amount": 5005, "currency": "USD"}}
        ]
        r = j.handle(
            {
                "tool": "post",
                "call_id": "c",
                "key": "k",
                "arguments": {"draft": {"postings": postings}},
            }
        )
        assert r.response == "invalid"
        with pytest.raises(ValueError, match="65536"):
            j.record_message("user", "x" * 65_537)
        j.close()


class TestPolicyRules:
    def test_rules_over_commands_without_an_amount_are_refused(self) -> None:
        with pytest.raises(ValueError, match="carries no single amount"):
            ThresholdPolicySet(version="v", deny_above=[Threshold("post", "USD", 0)])
        with pytest.raises(ValueError, match="carries no single amount"):
            ThresholdPolicySet(
                version="v", window_caps=[WindowCap("advance", "USD", 1, timedelta(hours=1))]
            )


class TestSchemaLinks:
    def test_a_decisions_consumption_must_be_of_its_own_presentation(self, tmp_path: Path) -> None:
        signer = generate_signing_key()
        pol = ThresholdPolicySet(
            version="p", approve_above=[Threshold("open_transaction", "USD", 100)]
        )
        j = _journal(tmp_path, policy=pol, approval_key=verification_key_text(signer))
        from ledgergate.journal import issue

        for key in ("a", "b"):
            j.handle(_open(key, 500))
        conn = sqlite3.connect(j.path)
        fps = dict(conn.execute("SELECT key, fingerprint FROM operations").fetchall())
        conn.close()
        for key in ("a", "b"):
            art = issue(
                signer,
                journal_id=j.definition.journal_id,
                approval_id=f"id-{key}",
                approver="cfo",
                fingerprint=fps[key],
                key=key,
                issued_at=EPOCH,
                expires_at=EPOCH + timedelta(days=1),
            ).to_json()
            assert (
                j.handle({**_open(key, 500), "call_id": f"ap-{key}", "approval": art}).response
                == "applied"
            )
        j.close()
        conn = sqlite3.connect(str(tmp_path / "j.journal"), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        decisions = conn.execute(
            "SELECT * FROM decisions WHERE consumption IS NOT NULL ORDER BY journal_sequence"
        ).fetchall()
        assert len(decisions) == 2
        d_a, d_b = decisions
        conn.execute("BEGIN")
        seq = conn.execute("INSERT INTO journal (kind) VALUES ('decisions')").lastrowid
        cross = (seq, *d_a[1:10], d_b[10])  # a's decision claiming b's consumption
        with pytest.raises(sqlite3.IntegrityError, match=r"own presentation|UNIQUE"):
            conn.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)", cross)
        conn.execute("ROLLBACK")
        conn.close()


def test_tokenizing_journal_rejects_admitter_swap_that_would_leak_raw_keys(tmp_path: Path) -> None:
    tk = Tokenizer(bytes(range(32)), domain="acme", key_version="v1")
    j = Journal.create(
        str(tmp_path / "t.journal"),
        CHART,
        clock=SteppingClock(EPOCH),
        ids=SequentialIds(),
        admitter=TokenizingAdmitter(tk),
    )
    assert (
        j.handle(
            {"tool": "post", "call_id": "c1", "key": "same-raw-key", "arguments": {"draft": SALE}}
        ).response
        == "applied"
    )
    with pytest.raises(ConfigurationError):
        j.admitter = IdentityAdmitter()
    assert (
        j.handle(
            {"tool": "post", "call_id": "c2", "key": "same-raw-key", "arguments": {"draft": SALE}}
        ).response
        == "replayed"
    )
    j.close()


class TestEverythingAdmittedIsRepresentable:
    """Derive-after-admit at each boundary the trace has."""

    def _derives(self, j: Journal) -> None:
        from ledgergate.derive import trace

        j.close()
        assert trace(j.path).journal_id is not None

    def test_tags_and_descriptions_at_the_bound(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        ok = {
            "draft": {**SALE, "description": "x" * 1024, "tags": {f"k{i}": "v" for i in range(100)}}
        }
        assert (
            j.handle({"tool": "post", "call_id": "c1", "key": "k1", "arguments": ok}).response
            == "applied"
        )
        too_long = {"draft": {**SALE, "description": "x" * 1025}}
        assert (
            j.handle({"tool": "post", "call_id": "c2", "key": "k2", "arguments": too_long}).response
            == "invalid"
        )
        too_many = {"draft": {**SALE, "tags": {f"k{i}": "v" for i in range(101)}}}
        assert (
            j.handle({"tool": "post", "call_id": "c3", "key": "k3", "arguments": too_many}).response
            == "invalid"
        )
        long_tag = {"draft": {**SALE, "tags": {"k": "v" * 1025}}}
        assert (
            j.handle({"tool": "post", "call_id": "c4", "key": "k4", "arguments": long_tag}).response
            == "invalid"
        )
        long_reverse = {
            "tool": "reverse",
            "call_id": "c5",
            "key": "k5",
            "arguments": {"entry_id": "e", "description": "x" * 1025},
        }
        assert j.handle(long_reverse).response == "invalid"
        self._derives(j)

    def test_a_chart_whose_trial_balance_would_not_fit_is_refused_at_create(
        self, tmp_path: Path
    ) -> None:
        big = ChartOfAccounts(
            [Account(f"a{i}", AccountType.ASSET, USD) for i in range(2000)]
            + [Account("rev", AccountType.REVENUE, USD)]
        )
        with pytest.raises(ConfigurationError, match="payload bound"):
            Journal.create(
                str(tmp_path / "big.journal"), big, clock=SteppingClock(EPOCH), ids=SequentialIds()
            )
        fits = ChartOfAccounts([Account(f"a{i}", AccountType.ASSET, USD) for i in range(1200)])
        j = Journal.create(
            str(tmp_path / "fits.journal"), fits, clock=SteppingClock(EPOCH), ids=SequentialIds()
        )
        assert (
            j.handle({"tool": "trial_balance", "call_id": "c", "arguments": {}}).response == "read"
        )
        self._derives(j)

    def test_a_policy_set_returning_an_overlong_reason_is_a_configuration_fault(
        self, tmp_path: Path
    ) -> None:
        from ledgergate.journal.policy import Decision, PolicyContext

        class Verbose(NullPolicySet):
            version = "verbose"

            def evaluate(self, context: PolicyContext) -> Decision:
                return Decision("allow", "verbose.rule", "r" * 2000)

        j = _journal(tmp_path, policy=Verbose())
        with pytest.raises(ConfigurationError, match="1024"):
            j.handle(_open("k", 5))
        j.close()


class TestContextsAreRepresentable:
    def test_a_long_account_name_is_refused_at_create(self, tmp_path: Path) -> None:
        chart = ChartOfAccounts([Account("cash", AccountType.ASSET, USD, name="n" * 2000)])
        with pytest.raises(ConfigurationError, match="account name"):
            Journal.create(
                str(tmp_path / "n.journal"), chart, clock=SteppingClock(EPOCH), ids=SequentialIds()
            )

    def test_a_set_returning_an_unusable_subject_or_aggregates_is_a_configuration_fault(
        self, tmp_path: Path
    ) -> None:
        from typing import Any as _Any

        from ledgergate.journal.policy import Decision, PolicyContext

        class OddSubject(NullPolicySet):
            version = "odd"

            def subject_of(self, command: _Any) -> str | None:
                return "s" * 300

            def evaluate(self, context: PolicyContext) -> Decision:
                return Decision("allow", "odd.ok", "fine")

        class OddAggregates(NullPolicySet):
            version = "odd2"

            def aggregates_for(self, command: _Any, now: _Any, history: _Any) -> dict[str, _Any]:
                return {"my.count": "abc"}

            def evaluate(self, context: PolicyContext) -> Decision:
                return Decision("allow", "odd2.ok", "fine")

        j = _journal(tmp_path, policy=OddSubject())
        with pytest.raises(ConfigurationError, match="subject"):
            j.handle(_open("k", 5))
        j.close()
        (tmp_path / "b").mkdir()
        j = _journal(tmp_path / "b", policy=OddAggregates())
        with pytest.raises(ConfigurationError, match="aggregates"):
            j.handle(_open("k", 5))
        j.close()
