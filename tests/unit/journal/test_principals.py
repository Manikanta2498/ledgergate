"""Authenticated principals and named approvers (docs/spec/principals.md, schema 7): the
registries, the auth envelope's clockless and clock checks, attribution on every row, check
1b, the trace additions and their invariant, and the CLI."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.unit.journal.support import CHART, rows

from ledgergate.cli.__main__ import main
from ledgergate.derive import trace as derive_trace
from ledgergate.invariants import check as verify
from ledgergate.journal import (
    ConfigurationError,
    IntegrityError,
    Journal,
    ThresholdPolicySet,
    generate_signing_key,
    issue,
    verification_key_text,
)
from ledgergate.journal.auth import (
    MAX_EXPIRY_SECONDS,
    AuthError,
    sign_request,
    signed_document,
    verify_envelope,
)
from ledgergate.journal.policy import Threshold
from ledgergate.ledger import EPOCH, SequentialIds, SteppingClock
from ledgergate.trace import dump_v2, load_any


def table(path: str, name: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(path)
    try:
        return rows(conn, name)
    finally:
        conn.close()


AGENT = generate_signing_key()
OTHER = generate_signing_key()
CFO = generate_signing_key()
CONTROLLER = generate_signing_key()


def post(key: str, amount: int = 5, call_id: str = "c1") -> dict[str, Any]:
    return {
        "tool": "post",
        "call_id": call_id,
        "key": key,
        "arguments": {
            "draft": {
                "postings": [
                    {
                        "account": "cash",
                        "side": "debit",
                        "money": {"amount": amount, "currency": "USD"},
                    },
                    {
                        "account": "revenue",
                        "side": "credit",
                        "money": {"amount": amount, "currency": "USD"},
                    },
                ]
            }
        },
    }


def signed(
    j: Journal,
    value: dict[str, Any],
    *,
    private: Any = AGENT,
    principal: str = "agent",
    seconds: int = 300,
) -> dict[str, Any]:
    clock = j.clock
    assert isinstance(clock, SteppingClock)
    env = sign_request(
        value,
        private=private,
        journal_id=j.definition.journal_id,
        principal=principal,
        expires_at=clock.peek() + timedelta(seconds=seconds),
    )
    return {**value, "auth": env}


@pytest.fixture
def j(tmp_path: Path) -> Any:
    journal = Journal.create(
        str(tmp_path / "p.journal"),
        CHART,
        clock=SteppingClock(EPOCH),
        ids=SequentialIds(),
        approvers={
            "cfo": verification_key_text(CFO),
            "controller": verification_key_text(CONTROLLER),
        },
    )
    journal.add_principal("agent", "signed", verification_key_text(AGENT))
    yield journal
    journal.close()


class TestRegistries:
    def test_create_seeds_the_bootstrap_and_the_approvers_in_one_transaction(
        self, j: Journal
    ) -> None:
        rows = table(j.path, "principal_events")
        assert [(r[1], r[2], r[3], r[5]) for r in rows] == [
            ("local", "add", "transport", "local"),
            ("agent", "add", "signed", "local"),
        ]
        assert [(r[1], r[2]) for r in table(j.path, "approver_events")] == [
            ("cfo", "add"),
            ("controller", "add"),
        ]
        # the definition and the seeds share the create transaction's sequence run
        kinds = [r[1] for r in table(j.path, "journal")][:4]
        assert kinds == ["definition", "principal_events", "approver_events", "approver_events"]

    def test_monotone_log_and_attribution_are_the_triggers_not_the_cli(self, j: Journal) -> None:
        with pytest.raises(ConfigurationError, match="already added"):
            j.add_principal("agent", "signed", verification_key_text(OTHER))
        j.revoke_principal("agent")
        with pytest.raises(ConfigurationError, match="already added"):
            j.add_principal(
                "agent", "signed", verification_key_text(OTHER)
            )  # a new key is a new name
        with pytest.raises(ConfigurationError, match="not live"):
            j.revoke_principal("agent")
        with pytest.raises(ConfigurationError, match="cannot revoke itself"):
            j.revoke_principal("local")
        # the trigger, bypassing the CLI pre-checks: a raw insert by a revoked or signed `by`
        conn = sqlite3.connect(j.path)
        try:
            (seq,) = conn.execute("SELECT MAX(journal_sequence) + 1 FROM journal").fetchone()
            conn.execute(
                "INSERT INTO journal (journal_sequence, kind) VALUES (?, 'principal_events')",
                (seq,),
            )
            with pytest.raises(sqlite3.IntegrityError, match="not a live transport principal"):
                conn.execute(
                    "INSERT INTO principal_events VALUES (?,?,?,?,?,?,?)",
                    (seq, "x", "add", "transport", None, "agent", EPOCH.isoformat()),
                )
            with pytest.raises(sqlite3.IntegrityError, match="cannot revoke itself"):
                conn.execute(
                    "INSERT INTO principal_events VALUES (?,?,?,?,?,?,?)",
                    (seq, "local", "revoke", None, None, "local", EPOCH.isoformat()),
                )
            with pytest.raises(sqlite3.IntegrityError):  # CHECK: a signed add needs a key
                conn.execute(
                    "INSERT INTO principal_events VALUES (?,?,?,?,?,?,?)",
                    (seq, "y", "add", "signed", None, "local", EPOCH.isoformat()),
                )
        finally:
            conn.close()

    def test_open_requires_a_live_transport_principal(self, j: Journal) -> None:
        j.add_principal("ops", "transport")
        j.close()
        with pytest.raises(ConfigurationError, match="not a live transport principal"):
            Journal.open(
                j.path, clock=SteppingClock(EPOCH), ids=SequentialIds(), principal="nobody"
            )
        with pytest.raises(ConfigurationError, match="not a live transport principal"):
            Journal.open(j.path, clock=SteppingClock(EPOCH), ids=SequentialIds(), principal="agent")
        ops = Journal.open(j.path, clock=SteppingClock(EPOCH), ids=SequentialIds(), principal="ops")
        try:
            ops.revoke_principal("local")  # by ops, live transport, not itself
            r = ops.handle(post("k1"))
            assert r.ok
        finally:
            ops.close()
        # the revoked session: refused at open, and mid-session as a recorded invalid
        with pytest.raises(ConfigurationError):
            Journal.open(j.path, clock=SteppingClock(EPOCH), ids=SequentialIds(), principal="local")

    def test_a_revoke_during_a_session_is_recorded_not_silent(
        self, j: Journal, tmp_path: Path
    ) -> None:
        j.add_principal("ops", "transport")
        other = Journal.open(
            j.path, clock=SteppingClock(EPOCH), ids=SequentialIds(), principal="ops"
        )
        try:
            other.revoke_principal("local")
        finally:
            other.close()
        r = j.handle(post("k1"))
        assert (r.disposition, r.error_type) == ("invalid", "revoked_principal")
        row = table(j.path, "invocations")[-1]
        assert (row[3], row[4]) == ("local", "transport")


class TestSignedRequests:
    def test_a_signed_request_is_attributed_to_the_signer(self, j: Journal) -> None:
        r = j.handle(signed(j, post("k1")))
        assert r.ok
        row = table(j.path, "invocations")[-1]
        assert (row[3], row[4], row[5]) == ("agent", "signed", "agent")
        assert row[6] is not None and row[7] is not None  # expiry and signature stored as evidence
        dec = table(j.path, "decisions")[-1]
        assert json.loads(dec[3])["principal"] == "agent"

    @pytest.mark.parametrize(
        ("mutate", "cause"),
        [
            (lambda v: v.__setitem__("auth", {"principal": "agent"}), "authentication_malformed"),
            (lambda v: v["auth"].__setitem__("signature", "short"), "authentication_malformed"),
            (
                lambda v: v["auth"].__setitem__("expires_at", "not a time"),
                "authentication_malformed",
            ),
            (lambda v: v["auth"].__setitem__("principal", "ghost"), "unknown_principal"),
            (
                lambda v: v["auth"].__setitem__("principal", "local"),
                "unknown_principal",
            ),  # a transport name is unknown as a signer
            (
                lambda v: v["arguments"]["draft"]["postings"][0]["money"].__setitem__("amount", 6),
                "bad_signature",
            ),
            (lambda v: v.__setitem__("key", "k2"), "bad_signature"),
            (lambda v: v.__setitem__("call_id", "c9"), "bad_signature"),
        ],
    )
    def test_clockless_refusals_are_rejected_rows_of_the_session(
        self, j: Journal, mutate: Any, cause: str
    ) -> None:
        v = signed(j, post("k1"))
        mutate(v)
        r = j.handle(v)
        assert (r.disposition, r.error_type, r.error_message) == ("invalid", cause, "auth")
        row = table(j.path, "invocations")[-1]
        assert (row[3], row[4], row[5]) == (
            "local",
            "rejected",
            None,
        )  # the claim is content, not a row
        assert "agent" not in (row[3], row[5])

    def test_a_signature_for_another_journal_or_key_does_not_verify(
        self, j: Journal, tmp_path: Path
    ) -> None:
        v = signed(j, post("k1"), private=OTHER)
        assert j.handle(v).error_type == "bad_signature"
        other = Journal.create(
            str(tmp_path / "o.journal"), CHART, clock=SteppingClock(EPOCH), ids=SequentialIds()
        )
        other.close()
        forged = sign_request(
            post("k1"),
            private=AGENT,
            journal_id="0" * 32,
            principal="agent",
            expires_at=EPOCH + timedelta(days=1),
        )
        assert j.handle({**post("k1"), "auth": forged}).error_type == "bad_signature"

    def test_expiry_and_bound_are_judged_at_the_single_reading_as_signed_rows(
        self, j: Journal
    ) -> None:
        clock = j.clock
        assert isinstance(clock, SteppingClock)
        # expires exactly at the reading the write will take: expired (<=), attributed to the signer
        v = signed(j, post("k1"), seconds=0)
        r = j.handle(v)
        assert (r.disposition, r.error_type) == ("invalid", "request_expired")
        row = table(j.path, "invocations")[-1]
        assert (row[3], row[4], row[5]) == ("agent", "signed", "agent")
        # valid for more than a day: refused as unbounded, a signed row too
        r = j.handle(signed(j, post("k1", call_id="c2"), seconds=MAX_EXPIRY_SECONDS + 1))
        assert r.error_type == "request_expiry_unbounded"
        # exactly at the bound is admitted
        r = j.handle(signed(j, post("k1", call_id="c3"), seconds=MAX_EXPIRY_SECONDS))
        assert r.ok

    def test_every_verified_envelope_is_spent_on_first_presentation(self, j: Journal) -> None:
        v = signed(j, post("k1"))
        assert j.handle(v).ok
        again = j.handle(v)  # the same bytes, twice
        assert (again.disposition, again.error_type) == ("invalid", "replayed_call")
        # a legitimate retry: new call id, same idempotency key -> replay of the operation
        retry = j.handle(signed(j, post("k1", call_id="c2")))
        assert (retry.disposition, retry.response) == ("replay", "replayed")
        # a rejected envelope for call id c5 spends nothing: the signer's later c5 is admitted
        bad = signed(j, post("k3", call_id="c5"))
        bad["auth"]["signature"] = "A" * 86
        assert j.handle(bad).error_type == "bad_signature"
        assert j.handle(signed(j, post("k3", call_id="c5"))).ok
        # a verified envelope the journal refused is spent too: expired, then re-presented
        # with a fresh window under the same call id -> replayed (not a bearer instrument)
        assert (
            j.handle(signed(j, post("k4", call_id="c6"), seconds=0)).error_type == "request_expired"
        )
        assert j.handle(signed(j, post("k4", call_id="c6"))).error_type == "replayed_call"
        # the UNIQUE is the guarantee: a raw duplicate pair is refused by the database
        conn = sqlite3.connect(j.path)
        try:
            (seq,) = conn.execute("SELECT MAX(journal_sequence) + 1 FROM journal").fetchone()
            conn.execute(
                "INSERT INTO journal (journal_sequence, kind) VALUES (?, 'signed_calls')", (seq,)
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO signed_calls VALUES (?,?,?,?)", (seq, "agent", "c1", 2))
        finally:
            conn.close()

    def test_a_refused_signed_request_is_not_a_bearer_instrument(self, j: Journal) -> None:
        # a signed reverse of an entry that does not exist yet: unknown_entry, signed row
        reverse = {
            "tool": "reverse",
            "call_id": "r1",
            "key": "rv",
            "arguments": {"entry_id": "e-000001"},
        }
        v = signed(j, reverse)
        assert j.handle(v).error_type == "unknown_entry"
        assert table(j.path, "invocations")[-1][4] == "signed"
        # the projection moves: the entry now exists
        assert j.handle(post("k1")).ok
        # the captured bytes, re-presented by anyone: spent, not honoured against the new ledger
        assert j.handle(v).error_type == "replayed_call"
        assert table(j.path, "outcomes")[-1][3] == "applied"  # only the post

    def test_a_signed_read_and_a_revoked_signer(self, j: Journal) -> None:
        r = j.handle(
            signed(j, {"tool": "balance", "call_id": "r1", "arguments": {"account": "cash"}})
        )
        assert r.ok and table(j.path, "invocations")[-1][4] == "signed"
        j.revoke_principal("agent")
        r = j.handle(signed(j, post("k9", call_id="c9")))
        assert r.error_type == "unknown_principal"

    def test_signed_document_is_the_request_as_delivered(self) -> None:
        doc = signed_document(
            {"tool": "balance", "call_id": "r1", "auth": {"x": 1}},
            journal_id="j",
            principal="p",
            expires_at="t",
        )
        assert doc == {
            "request": {"tool": "balance", "call_id": "r1"},
            "journal_id": "j",
            "principal": "p",
            "expires_at": "t",
        }  # absent is absent, not null; auth excluded; nested, so nothing collides
        with pytest.raises(AuthError, match="authentication_malformed"):
            verify_envelope(
                {},
                {"principal": "p", "expires_at": "x", "signature": "A" * 86, "extra": 1},
                signers={},
                journal_id="j",
            )


class TestNamedApprovers:
    def _gated(self, tmp_path: Path, approvers: tuple[str, ...] | None) -> Journal:
        policy = ThresholdPolicySet(
            version="v1",
            approve_above=[Threshold("open_transaction", "USD", 5_000, approvers)],
        )
        return Journal.create(
            str(tmp_path / "g.journal"),
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=policy,
            approvers={
                "cfo": verification_key_text(CFO),
                "controller": verification_key_text(CONTROLLER),
            },
        )

    def _artefact(self, j: Journal, private: Any, approver: str, key: str = "k1") -> dict[str, Any]:
        (op,) = [r for r in table(j.path, "operations") if r[1] == key]
        return issue(
            private,
            journal_id=j.definition.journal_id,
            approval_id=f"appr-{approver}",
            approver=approver,
            fingerprint=op[2],
            key=key,
            issued_at=EPOCH,
            expires_at=EPOCH + timedelta(days=1),
        ).to_json()

    def _open(
        self, key: str, call_id: str, approval: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        v: dict[str, Any] = {
            "tool": "open_transaction",
            "call_id": call_id,
            "key": key,
            "arguments": {"transaction_id": "t-big", "amount": {"amount": 6000, "currency": "USD"}},
        }
        if approval is not None:
            v["approval"] = approval
        return v

    def test_check_1_uses_the_key_registered_under_the_label(self, tmp_path: Path) -> None:
        j = self._gated(tmp_path, None)
        try:
            assert j.handle(self._open("k1", "c1")).response == "awaiting_approval"
            # the controller's key under the cfo label: the label is authenticated, so invalid
            forged = self._artefact(j, CONTROLLER, "cfo")
            r = j.handle(self._open("k1", "c2", forged))
            assert r.error_message == "runtime.approval_rejected: approval_invalid"
            # an unregistered label with a valid key: invalid too
            r = j.handle(self._open("k1", "c3", self._artefact(j, CFO, "ceo")))
            assert r.error_message == "runtime.approval_rejected: approval_invalid"
            # the right key under the right label, any approver admitted
            r = j.handle(self._open("k1", "c4", self._artefact(j, CONTROLLER, "controller")))
            assert r.response == "applied"
            dec = json.loads(table(j.path, "decisions")[-1][3])
            assert dec["approval"]["approver"] == "controller"
        finally:
            j.close()

    def test_check_1b_refuses_a_registered_approver_the_line_does_not_admit(
        self, tmp_path: Path
    ) -> None:
        j = self._gated(tmp_path, ("cfo",))
        try:
            assert j.handle(self._open("k1", "c1")).response == "awaiting_approval"
            r = j.handle(self._open("k1", "c2", self._artefact(j, CONTROLLER, "controller")))
            assert (r.response, r.error_message) == (
                "awaiting_approval",
                "runtime.approval_rejected: approval_wrong_approver",
            )
            pres = table(j.path, "approvals")[-1]
            assert pres[13] == 1 and pres[14] == "approval_wrong_approver"  # verified, 1b's result
            assert table(j.path, "approval_consumptions") == []
            dec = json.loads(table(j.path, "decisions")[-1][3])
            assert dec["approval"] == {
                "presentation": pres[0],
                "verdict": "approval_wrong_approver",
                "approver": "controller",
            }
            ok = j.handle(self._open("k1", "c3", self._artefact(j, CFO, "cfo")))
            assert ok.response == "applied"
            # the trace: verify recomputes 1b and the attribution invariant
            t = derive_trace(j.path)
            card = verify(t)
            assert card.status == "pass"
            assert {r.name: r.status for r in card.results}["attributions_are_registered"] == "pass"
        finally:
            j.close()

    def test_a_revoked_approver_no_longer_verifies_and_the_line_is_stranded(
        self, tmp_path: Path
    ) -> None:
        j = self._gated(tmp_path, ("cfo",))
        try:
            j.handle(self._open("k1", "c1"))
            j.revoke_approver("cfo")
            r = j.handle(self._open("k1", "c2", self._artefact(j, CFO, "cfo")))
            assert r.error_message == "runtime.approval_rejected: approval_invalid"
        finally:
            j.close()

    def test_create_refusals(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="admits nobody"):
            Threshold("open_transaction", "USD", 1, ())
        with pytest.raises(ValueError, match="deny line admits no approver"):
            ThresholdPolicySet(
                version="v", deny_above=[Threshold("open_transaction", "USD", 1, ("cfo",))]
            )
        policy = ThresholdPolicySet(
            version="v1", approve_above=[Threshold("open_transaction", "USD", 5, ("ceo",))]
        )
        with pytest.raises(ConfigurationError, match="not seeded"):
            Journal.create(
                str(tmp_path / "x"),
                CHART,
                clock=SteppingClock(EPOCH),
                ids=SequentialIds(),
                policy=policy,
                approvers={"cfo": verification_key_text(CFO)},
            )
        with pytest.raises(ConfigurationError, match="no approver is seeded"):
            Journal.create(
                str(tmp_path / "y"),
                CHART,
                clock=SteppingClock(EPOCH),
                ids=SequentialIds(),
                policy=policy,
            )
        # the digest of a configuration without the field is unchanged from schema 6's shape
        plain = ThresholdPolicySet(
            version="v1", approve_above=[Threshold("open_transaction", "USD", 5)]
        )
        assert "approvers" not in plain.configuration()["approve_above"][0]


class TestTraceAndInvariant:
    def test_derived_trace_carries_attribution_registry_events_and_causes(self, j: Journal) -> None:
        j.handle(signed(j, post("k1")))
        bad = signed(j, post("k2", call_id="c2"))
        bad["auth"]["signature"] = "A" * 86
        j.handle(bad)
        j.handle({"tool": "nope", "call_id": "c3"})
        t = derive_trace(j.path)
        res = [e for e in t.events if e.type == "invocation_resolution"]
        assert [(r.principal, r.authentication, r.error_type) for r in res] == [
            ("agent", "signed", None),
            ("local", "rejected", "bad_signature"),
            ("local", "transport", "unknown_tool"),
        ]
        assert [e.type for e in t.events[:2]] == ["principal_change", "approver_change"]
        card = verify(t)
        assert {r.name: r.status for r in card.results}["attributions_are_registered"] == "pass"
        assert {r.name: r.status for r in card.results}[
            "committed_response_matches_journal"
        ] == "pass"
        # the document round-trips and a forged attribution is caught
        doc = json.loads(dump_v2(t))
        forged = json.loads(json.dumps(doc))
        for e in forged["events"]:
            if e["type"] == "invocation_resolution" and e["principal"] == "agent":
                e["principal"] = "ghost"
        card = verify(load_any(json.dumps(forged)))
        assert {r.name: r.status for r in card.results}["attributions_are_registered"] == "fail"
        # a pre-schema-7 document (no attribution) loads with AdmissionError and no evidence
        older = json.loads(json.dumps(doc))
        older["events"] = [e for e in older["events"] if not e["type"].endswith("_change")]
        for e in older["events"]:
            if e["type"] == "invocation_resolution":
                e.pop("principal"), e.pop("authentication"), e.pop("error_type", None)
            if e["type"] == "tool_result" and not e["ok"]:
                e["error"]["type"] = "AdmissionError"
        for i, e in enumerate(older["events"], start=1):
            e["seq"] = i
        old = load_any(json.dumps(older))
        assert {r.name: r.status for r in verify(old).results}[
            "attributions_are_registered"
        ] == "no_evidence"


class TestCli:
    def test_keygen_sign_registry_and_approve(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seed = tmp_path / "agent.seed"
        assert main(["keygen", "--seed-file", str(seed)]) == 0
        pub = capsys.readouterr().out.strip()
        (tmp_path / "agent.pub").write_text(pub)
        assert main(["keygen", "--seed-file", str(seed)]) == 2  # never overwrite a seed
        cfo_seed = tmp_path / "cfo.seed"
        assert main(["keygen", "--seed-file", str(cfo_seed)]) == 0
        (tmp_path / "cfo.pub").write_text(capsys.readouterr().out.strip())
        chart = tmp_path / "chart.json"
        chart.write_text(
            json.dumps(
                [
                    {"account_id": "cash", "kind": "asset", "currency": "USD"},
                    {"account_id": "revenue", "kind": "revenue", "currency": "USD"},
                ]
            )
        )
        journal = tmp_path / "j.journal"
        # create through serve --create with a seeded approver, then exit on EOF
        import subprocess
        import sys

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "ledgergate.cli",
                "serve",
                "--journal",
                str(journal),
                "--create",
                "--chart",
                str(chart),
                "--approver",
                f"cfo={tmp_path / 'cfo.pub'}",
            ],
            input=b"",
            capture_output=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert main(["journal", "approver", "list", str(journal)]) == 0
        assert '"name": "cfo"' in capsys.readouterr().out
        assert (
            main(
                [
                    "journal",
                    "principal",
                    "add",
                    str(journal),
                    "agent",
                    "--verification-key-file",
                    str(tmp_path / "agent.pub"),
                ]
            )
            == 0
        )
        assert (
            main(
                [
                    "journal",
                    "principal",
                    "add",
                    str(journal),
                    "agent",
                    "--verification-key-file",
                    str(tmp_path / "agent.pub"),
                ]
            )
            == 2
        )
        assert "already added" in capsys.readouterr().err
        assert (
            main(["journal", "principal", "add", str(journal), "ops", "--kind", "transport"]) == 0
        )
        assert main(["journal", "principal", "revoke", str(journal), "local"]) == 2  # itself
        assert (
            main(["journal", "principal", "revoke", str(journal), "local", "--principal", "ops"])
            == 0
        )
        assert main(["journal", "id", str(journal)]) == 0
        jid = capsys.readouterr().out.strip()
        request = tmp_path / "req.json"
        request.write_text(json.dumps(post("k1")))
        assert (
            main(
                [
                    "sign",
                    str(request),
                    "--seed-file",
                    str(seed),
                    "--journal-id",
                    jid,
                    "--principal",
                    "agent",
                ]
            )
            == 0
        )
        envelope = json.loads(capsys.readouterr().out)
        assert set(envelope) == {"principal", "expires_at", "signature"}
        assert (
            main(
                [
                    "sign",
                    str(request),
                    "--seed-file",
                    str(seed),
                    "--journal-id",
                    jid,
                    "--principal",
                    "agent",
                    "--expires-in",
                    "999999",
                ]
            )
            == 2
        )
        # the signed request through serve, as a client would send it
        line = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "post",
                    "arguments": {**post("k1")["arguments"], "idempotency_key": "k1"},
                    "_meta": {"ledgergate": envelope},
                },
            }
        )
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "ledgergate.cli",
                "serve",
                "--journal",
                str(journal),
                "--principal",
                "ops",
            ],
            input=(line + "\n").encode(),
            capture_output=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout.splitlines()[0])
        # the envelope was signed for call_id "c1" while serve derives rpc-n1: bad_signature,
        # exactly the binding the spec promises; a client signs the call id it will send
        assert out["result"]["structuredContent"]["error"]["type"] == "bad_signature"
        request.write_text(json.dumps(post("k1", call_id="rpc-n1")))
        assert (
            main(
                [
                    "sign",
                    str(request),
                    "--seed-file",
                    str(seed),
                    "--journal-id",
                    jid,
                    "--principal",
                    "agent",
                ]
            )
            == 0
        )
        envelope = json.loads(capsys.readouterr().out)
        line = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "post",
                    "arguments": {**post("k1")["arguments"], "idempotency_key": "k1"},
                    "_meta": {"ledgergate": envelope},
                },
            }
        )
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "ledgergate.cli",
                "serve",
                "--journal",
                str(journal),
                "--principal",
                "ops",
            ],
            input=(line + "\n").encode(),
            capture_output=True,
            check=False,
        )
        out = json.loads(proc.stdout.splitlines()[0])
        assert out["result"]["structuredContent"]["ok"] is True, out
        assert main(["verify", str(journal)]) == 0
        assert "attributions_are_registered" in capsys.readouterr().out


class TestFirstImplementationReview:
    def test_registry_cli_works_on_a_policy_bound_tokenizing_journal(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import subprocess
        import sys

        chart = tmp_path / "chart.json"
        chart.write_text(
            json.dumps(
                [
                    {"account_id": "cash", "kind": "asset", "currency": "USD"},
                    {"account_id": "revenue", "kind": "revenue", "currency": "USD"},
                ]
            )
        )
        policy = tmp_path / "policy.json"
        policy.write_text(
            json.dumps(
                {
                    "set": "ledgergate.journal.policy.ThresholdPolicySet",
                    "version": "p1",
                    "deny_above": [],
                    "approve_above": [
                        {"kind": "open_transaction", "currency": "USD", "amount": "5000"}
                    ],
                    "window_caps": [],
                    "gated_reads": [],
                }
            )
        )
        token_key = tmp_path / "token.key"
        token_key.write_bytes(bytes(range(32)))
        (tmp_path / "cfo.pub").write_text(verification_key_text(CFO))
        (tmp_path / "agent.pub").write_text(verification_key_text(AGENT))
        journal = tmp_path / "bound.journal"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "ledgergate.cli",
                "serve",
                "--journal",
                str(journal),
                "--create",
                "--chart",
                str(chart),
                "--policy",
                str(policy),
                "--token-key-file",
                str(token_key),
                "--approver",
                f"cfo={tmp_path / 'cfo.pub'}",
            ],
            input=b"",
            capture_output=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        # the registry commands need neither the policy nor the token key: a registry
        # transaction runs neither, and the operator's liveness is what binds
        assert (
            main(
                [
                    "journal",
                    "principal",
                    "add",
                    str(journal),
                    "agent",
                    "--verification-key-file",
                    str(tmp_path / "agent.pub"),
                ]
            )
            == 0
        )
        assert (
            main(
                [
                    "journal",
                    "approver",
                    "add",
                    str(journal),
                    "controller",
                    "--verification-key-file",
                    str(tmp_path / "agent.pub"),
                ]
            )
            == 0
        )
        assert main(["journal", "approver", "revoke", str(journal), "controller"]) == 0
        assert main(["journal", "approver", "list", str(journal)]) == 0
        out = capsys.readouterr().out
        assert out.count('"name": "controller"') == 2  # add, revoke
        # but such a journal cannot handle or record in registry-only mode
        from ledgergate.mcp.effects import RandomIds, SystemClock

        ro = Journal.open(str(journal), clock=SystemClock(), ids=RandomIds(), registry_only=True)
        try:
            with pytest.raises(ConfigurationError, match="registry changes only"):
                ro.handle(post("k1"))
            with pytest.raises(ConfigurationError, match="registry changes only"):
                ro.record_message("user", "x")
        finally:
            ro.close()

    def test_journal_pending_shows_admitted_approvers_and_stranding(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        policy = ThresholdPolicySet(
            version="v1",
            approve_above=[Threshold("open_transaction", "USD", 5_000, ("cfo",))],
        )
        j = Journal.create(
            str(tmp_path / "g.journal"),
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=policy,
            approvers={
                "cfo": verification_key_text(CFO),
                "controller": verification_key_text(CONTROLLER),
            },
        )
        try:
            j.handle(
                {
                    "tool": "open_transaction",
                    "call_id": "c1",
                    "key": "k1",
                    "arguments": {
                        "transaction_id": "t",
                        "amount": {"amount": 6000, "currency": "USD"},
                    },
                }
            )
            assert main(["journal", "pending", j.path]) == 0
            row = json.loads(capsys.readouterr().out.strip())
            assert row["admitted_approvers"] == {"cfo": True} and row["stranded"] is False
            j.revoke_approver("cfo")
            assert main(["journal", "pending", j.path]) == 0
            row = json.loads(capsys.readouterr().out.strip())
            assert row["admitted_approvers"] == {"cfo": False} and row["stranded"] is True
        finally:
            j.close()

    def test_a_forged_registry_log_is_not_a_second_answer(self, j: Journal) -> None:
        j.handle(signed(j, post("k1")))
        doc = json.loads(dump_v2(derive_trace(j.path)))
        # a second `add` for agent, as transport, appended before the signed call
        forged = json.loads(json.dumps(doc))
        idx = next(i for i, e in enumerate(forged["events"]) if e["type"] == "tool_call")
        extra = {
            **next(
                e
                for e in forged["events"]
                if e["type"] == "principal_change" and e["name"] == "agent"
            )
        }
        extra["kind"] = "transport"
        forged["events"].insert(idx, extra)
        for i, e in enumerate(forged["events"], start=1):
            e["seq"] = i
        card = verify(load_any(json.dumps(forged)))
        findings = {r.name: r.status for r in card.results}
        assert findings["attributions_are_registered"] == "fail"
        # a message before the bootstrap: the exemption is the document's first event only
        shifted = json.loads(json.dumps(doc))
        first = shifted["events"][0]
        shifted["events"].insert(
            0, {"type": "message", "seq": 1, "at": first["at"], "role": "user", "content": "hi"}
        )
        for i, e in enumerate(shifted["events"], start=1):
            e["seq"] = i
        card = verify(load_any(json.dumps(shifted)))
        assert {r.name: r.status for r in card.results}["attributions_are_registered"] == "fail"

    @pytest.mark.parametrize(
        "stamp", ["2026-01-01 00:01:00+00:00", "20260101T000100+0000", "2026-01-01T00:01:00"]
    )
    def test_expires_at_grammar_is_the_extended_rfc3339_form_with_an_offset(
        self, j: Journal, stamp: str
    ) -> None:
        v = signed(j, post("k1"))
        v["auth"]["expires_at"] = stamp
        assert j.handle(v).error_type == "authentication_malformed"

    def test_signature_spelling_is_canonical(self, j: Journal) -> None:
        v = signed(j, post("k1"))
        sig = v["auth"]["signature"]
        # flip a padding bit in the last character: decodes to the same bytes, not canonical
        last = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        i = last.index(sig[-1])
        v["auth"]["signature"] = sig[:-1] + last[i ^ 1]
        assert j.handle(v).error_type == "authentication_malformed"


class TestSecondImplementationReview:
    def test_a_stripped_attribution_and_a_disagreeing_context_are_forgeries(
        self, j: Journal
    ) -> None:
        j.handle(signed(j, post("k1")))
        doc = json.loads(dump_v2(derive_trace(j.path)))
        stripped = json.loads(json.dumps(doc))
        for e in stripped["events"]:
            if e["type"] == "invocation_resolution":
                e.pop("principal"), e.pop("authentication"), e.pop("error_type", None)
        card = verify(load_any(json.dumps(stripped)))
        assert {r.name: r.status for r in card.results}["attributions_are_registered"] == "fail"
        disagree = json.loads(json.dumps(doc))
        for e in disagree["events"]:
            if e["type"] == "policy_decision":
                e["context"]["principal"] = "local"
        card = verify(load_any(json.dumps(disagree)))
        assert {r.name: r.status for r in card.results}["attributions_are_registered"] == "fail"

    def test_cli_refusals(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        j = Journal.create(
            str(tmp_path / "r.journal"), CHART, clock=SteppingClock(EPOCH), ids=SequentialIds()
        )
        j.close()
        pub = tmp_path / "k.pub"
        pub.write_text(verification_key_text(AGENT))
        assert (
            main(
                [
                    "journal",
                    "principal",
                    "add",
                    str(tmp_path / "r.journal"),
                    "ops",
                    "--kind",
                    "transport",
                    "--verification-key-file",
                    str(pub),
                ]
            )
            == 2
        )
        assert "takes no key" in capsys.readouterr().err
        seed = tmp_path / "s.seed"
        assert main(["keygen", "--seed-file", str(seed)]) == 0
        capsys.readouterr()
        req = tmp_path / "req.json"
        req.write_text(json.dumps(post("k1")))
        assert (
            main(
                [
                    "sign",
                    str(req),
                    "--seed-file",
                    str(seed),
                    "--journal-id",
                    "0" * 32,
                    "--principal",
                    "bad\nname",
                ]
            )
            == 2
        )
        req.write_text('{"tool": "post", "call_id": "c", "arguments": {"x": 1e400}}')
        assert (
            main(
                [
                    "sign",
                    str(req),
                    "--seed-file",
                    str(seed),
                    "--journal-id",
                    "0" * 32,
                    "--principal",
                    "agent",
                ]
            )
            == 2
        )
        chart = tmp_path / "chart.json"
        chart.write_text(json.dumps([{"account_id": "cash", "kind": "asset", "currency": "USD"}]))
        assert (
            main(
                [
                    "serve",
                    "--journal",
                    str(tmp_path / "x.journal"),
                    "--create",
                    "--chart",
                    str(chart),
                    "--approver",
                    f"cfo={pub}",
                    "--approver",
                    f"cfo={pub}",
                ]
            )
            == 2
        )
        assert "twice" in capsys.readouterr().err


class TestThirdImplementationReview:
    def test_a_trace_with_attributions_and_no_registry_fails_rather_than_switching_off(
        self, j: Journal
    ) -> None:
        j.handle(signed(j, post("k1")))
        doc = json.loads(dump_v2(derive_trace(j.path)))
        stripped = json.loads(json.dumps(doc))
        stripped["events"] = [e for e in stripped["events"] if not e["type"].endswith("_change")]
        for i, e in enumerate(stripped["events"], start=1):
            e["seq"] = i
        card = verify(load_any(json.dumps(stripped)))
        assert {r.name: r.status for r in card.results}["attributions_are_registered"] == "fail"

    def test_the_trace_models_cause_set_is_what_admission_raises(self) -> None:
        import re

        from ledgergate.trace.v2 import ADMISSION_CAUSES

        src = Path("src/ledgergate/journal")
        raised = set()
        for f in ("admission.py", "store.py"):
            raised |= set(re.findall(r'AdmissionError\("([a-z_]+)"', (src / f).read_text()))
        raised |= set(re.findall(r'AuthError\("([a-z_]+)"', (src / "auth.py").read_text()))
        raised |= set(
            re.findall(
                r'"(request_expired|request_expiry_unbounded|replayed_call)"',
                (src / "auth.py").read_text() + (src / "store.py").read_text(),
            )
        )
        assert raised <= ADMISSION_CAUSES
        assert ADMISSION_CAUSES - raised == set(), ADMISSION_CAUSES - raised


class TestFourthImplementationReview:
    @pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"])
    def test_calendar_edge_stamps_are_malformed_not_crashes(self, j: Journal, stamp: str) -> None:
        v = signed(j, post("k1"))
        v["auth"]["expires_at"] = stamp
        assert j.handle(v).error_type == "authentication_malformed"
        art = {
            "journal_id": "0" * 32,
            "approval_id": "a",
            "approver": "cfo",
            "fingerprint": "0" * 64,
            "key": "k1",
            "subject": None,
            "amount": None,
            "currency": None,
            "issued_at": stamp,
            "expires_at": stamp,
            "signature": "A" * 86,
        }
        r = j.handle({**post("k2", call_id="c2"), "approval": art})
        assert r.error_type == "approval_malformed"

    def test_an_attached_member_breaks_the_signature_rather_than_being_attributed(
        self, j: Journal
    ) -> None:
        v = signed(j, post("k1"))
        v["smuggled"] = 1
        r = j.handle(v)
        assert (r.error_type, table(j.path, "invocations")[-1][4]) == ("bad_signature", "rejected")
        assert j.handle(signed(j, post("k1"))).ok  # the genuine request still applies

    def test_context_approver_is_tied_to_the_presentation(self, tmp_path: Path) -> None:
        policy = ThresholdPolicySet(
            version="v1", approve_above=[Threshold("open_transaction", "USD", 5_000)]
        )
        j = Journal.create(
            str(tmp_path / "g.journal"),
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            policy=policy,
            approvers={
                "cfo": verification_key_text(CFO),
                "controller": verification_key_text(CONTROLLER),
            },
        )
        try:
            opener = {
                "tool": "open_transaction",
                "call_id": "c1",
                "key": "k1",
                "arguments": {"transaction_id": "t", "amount": {"amount": 6000, "currency": "USD"}},
            }
            j.handle(opener)
            (op,) = table(j.path, "operations")
            art = issue(
                CONTROLLER,
                journal_id=j.definition.journal_id,
                approval_id="a1",
                approver="controller",
                fingerprint=op[2],
                key="k1",
                issued_at=EPOCH,
                expires_at=EPOCH + timedelta(days=1),
            ).to_json()
            assert j.handle({**opener, "call_id": "c2", "approval": art}).response == "applied"
            doc = json.loads(dump_v2(derive_trace(j.path)))
        finally:
            j.close()
        for forged_value in ("cfo", None):
            forged = json.loads(json.dumps(doc))
            for e in forged["events"]:
                if e["type"] == "policy_decision" and e["context"].get("approval"):
                    e["context"]["approval"]["approver"] = forged_value
            card = verify(load_any(json.dumps(forged)))
            assert {r.name: r.status for r in card.results}[
                "attributions_are_registered"
            ] == "fail", forged_value

    def test_revoked_principal_rows_are_transport_attributed(self, j: Journal) -> None:
        j.add_principal("ops", "transport")
        other = Journal.open(
            j.path, clock=SteppingClock(EPOCH), ids=SequentialIds(), principal="ops"
        )
        try:
            other.revoke_principal("local")
        finally:
            other.close()
        j.handle(post("k1"))
        doc = json.loads(dump_v2(derive_trace(j.path)))
        forged = json.loads(json.dumps(doc))
        for e in forged["events"]:
            if e["type"] == "invocation_resolution" and e.get("error_type") == "revoked_principal":
                e["authentication"] = "signed"
        card = verify(load_any(json.dumps(forged)))
        assert {r.name: r.status for r in card.results}["attributions_are_registered"] == "fail"


class TestFifthImplementationReview:
    @pytest.mark.parametrize("member", ["arguments", "key", "approval"])
    def test_an_attached_null_member_breaks_the_signature(self, j: Journal, member: str) -> None:
        v = signed(j, {"tool": "trial_balance", "call_id": "r1"})
        v[member] = None
        r = j.handle(v)
        assert (r.error_type, table(j.path, "invocations")[-1][4]) == ("bad_signature", "rejected")
        assert j.handle(signed(j, {"tool": "trial_balance", "call_id": "r1"})).ok


class TestSixthImplementationReview:
    @pytest.mark.parametrize("member", ["journal_id", "principal", "expires_at"])
    def test_an_attached_envelope_named_member_breaks_the_signature(
        self, j: Journal, member: str
    ) -> None:
        v = signed(j, {"tool": "trial_balance", "call_id": "r1"})
        v[member] = v["auth"].get(member, j.definition.journal_id)
        r = j.handle(v)
        assert (r.error_type, table(j.path, "invocations")[-1][4]) == ("bad_signature", "rejected")
        assert j.handle(signed(j, {"tool": "trial_balance", "call_id": "r1"})).ok

    def test_sign_request_refuses_a_request_that_already_carries_auth(self, j: Journal) -> None:
        with pytest.raises(ValueError, match="already carries"):
            signed(j, {**post("k1"), "auth": {}})

    def test_revoked_principal_row_naming_a_signed_add_is_forged(self, j: Journal) -> None:
        j.add_principal("ops", "transport")
        other = Journal.open(
            j.path, clock=SteppingClock(EPOCH), ids=SequentialIds(), principal="ops"
        )
        try:
            other.revoke_principal("agent")  # a signed principal, revoked first
            other.revoke_principal("local")
        finally:
            other.close()
        j.handle(post("k1"))  # the revoked_principal row, after both revokes
        doc = json.loads(dump_v2(derive_trace(j.path)))
        assert verify(load_any(json.dumps(doc))).status == "pass"
        forged = json.loads(json.dumps(doc))
        for e in forged["events"]:
            if e["type"] == "invocation_resolution" and e.get("error_type") == "revoked_principal":
                e["principal"] = "agent"  # has a revoke before the row, but was never transport
        card = verify(load_any(json.dumps(forged)))
        assert {r.name: r.status for r in card.results}["attributions_are_registered"] == "fail"


class TestEighthImplementationReview:
    def test_a_second_presentation_is_always_replayed_call_even_when_admission_would_fail_again(
        self, j: Journal
    ) -> None:
        reverse = {
            "tool": "reverse",
            "call_id": "r1",
            "key": "rv",
            "arguments": {"entry_id": "e-9"},
        }
        v = signed(j, reverse)
        assert j.handle(v).error_type == "unknown_entry"
        assert j.handle(v).error_type == "replayed_call"  # not unknown_entry twice
        conn = sqlite3.connect(j.path)
        try:
            (n,) = conn.execute("SELECT COUNT(*) FROM signed_calls").fetchone()
        finally:
            conn.close()
        assert n == 1  # the replayed_call row spent nothing: the pair was already there

    def test_the_spend_is_an_unconditional_insert_the_unique_backs(
        self, j: Journal, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # if the step-3 check were skipped, the constraint, not the SELECT, would stop a replay
        v = signed(j, post("k1"))
        assert j.handle(v).ok
        monkeypatch.setattr(Journal, "_signed_call_spent", lambda self, p, c: False)
        with pytest.raises(IntegrityError):
            j.handle(v)
        # and the failed transaction left nothing behind
        assert table(j.path, "invocations")[-1][12] is not None
        assert len([r for r in table(j.path, "invocations") if r[8] != "invalid"]) == 1

    def test_a_non_identifier_call_id_on_a_verified_envelope_spends_nothing(
        self, j: Journal
    ) -> None:
        v = signed(j, {**post("k1"), "call_id": "two\nlines"})
        r = j.handle(v)
        assert (r.error_type, table(j.path, "invocations")[-1][4]) == (
            "invalid_identifier",
            "signed",
        )
        conn = sqlite3.connect(j.path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM signed_calls").fetchone() == (0,)
        finally:
            conn.close()
