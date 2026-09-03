"""The M2c admitter behind the M2b seam: every caller identifier tokenized on every
reference, every free-text field redacted, before anything is fingerprinted or written."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.unit.journal.support import post, rows

from ledgergate.codec import REDACTION_PATTERN, TOKEN_PATTERN, Tokenizer
from ledgergate.journal import ConfigurationError, IdentityAdmitter, Journal, TokenizingAdmitter
from ledgergate.ledger import (
    EPOCH,
    USD,
    Account,
    AccountType,
    ChartOfAccounts,
    SequentialIds,
    SteppingClock,
)

KEY = bytes(range(32))
TK = Tokenizer(KEY, domain="acme", key_version="v1")
CHART = ChartOfAccounts(
    [
        Account("cash", AccountType.ASSET, USD, name="Alice Smith's wallet"),
        Account("revenue", AccountType.REVENUE, USD),
    ]
)
SECRETS = ("txn-alice", "alice@example.com", "SO-1", "order-42", "call-7", "Alice Smith")


def sale(**extra: Any) -> dict[str, Any]:
    return {
        "postings": [
            {"account": "cash", "side": "debit", "money": {"amount": 1999, "currency": "USD"}},
            {"account": "revenue", "side": "credit", "money": {"amount": 1999, "currency": "USD"}},
        ],
        **extra,
    }


@pytest.fixture
def tokenizing(tmp_path: Path) -> Iterator[Journal]:
    j = Journal.create(
        str(tmp_path / "t.journal"),
        CHART,
        clock=SteppingClock(EPOCH),
        ids=SequentialIds(),
        admitter=TokenizingAdmitter(TK),
    )
    yield j
    j.close()


def table(path: str, name: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(path)
    try:
        return rows(conn, name)
    finally:
        conn.close()


def everything_stored(path: str) -> str:
    conn = sqlite3.connect(path)
    try:
        return " ".join(
            json.dumps(row, default=str)
            for table in (
                "definition",
                "operations",
                "invocations",
                "events",
                "outcomes",
                "decisions",
            )
            for row in conn.execute(f"SELECT * FROM {table}")
        )
    finally:
        conn.close()


class TestNoRawValueReachesStorage:
    def test_a_full_lifecycle_stores_only_tokens_and_redactions(self, tokenizing: Journal) -> None:
        j = tokenizing
        amount = {"amount": 1999, "currency": "USD"}
        r1 = j.handle(
            {
                "tool": "open_transaction",
                "call_id": "call-7",
                "key": "order-42",
                "arguments": {"transaction_id": "txn-alice", "amount": amount},
            }
        )
        r2 = j.handle(
            {
                "tool": "advance",
                "call_id": "c2",
                "key": "k2",
                "arguments": {"transaction_id": "txn-alice", "event": "authorize"},
            }
        )
        r3 = j.handle(
            {
                "tool": "advance",
                "call_id": "c3",
                "key": "k3",
                "arguments": {
                    "transaction_id": "txn-alice",
                    "event": "settle",
                    "entry": sale(description="alice@example.com", tags={"order": "SO-1"}),
                },
            }
        )
        r4 = j.handle(
            {
                "tool": "refund",
                "call_id": "c4",
                "key": "k4",
                "arguments": {
                    "transaction_id": "txn-alice",
                    "money": {"amount": 500, "currency": "USD"},
                    "entry": {
                        "postings": [
                            {
                                "account": "revenue",
                                "side": "debit",
                                "money": {"amount": 500, "currency": "USD"},
                            },
                            {
                                "account": "cash",
                                "side": "credit",
                                "money": {"amount": 500, "currency": "USD"},
                            },
                        ]
                    },
                },
            }
        )
        assert [r.response for r in (r1, r2, r3, r4)] == ["applied"] * 4
        # the same raw transaction id found the same stored transaction every time
        assert r4.result["transaction"]["id"] == TK.tokenize("txn-alice")
        assert r4.result["transaction"]["status"] == "partially_refunded"
        stored = everything_stored(j.path)
        for secret in SECRETS:
            assert secret not in stored, secret
        assert "cash" in stored and "1999" in stored  # the books stay in the clear

    def test_retry_with_the_raw_key_replays(self, tokenizing: Journal) -> None:
        first = tokenizing.handle(post("order-42", call_id="c1", description="alice@example.com"))
        again = tokenizing.handle(post("order-42", call_id="c2", description="alice@example.com"))
        assert (first.response, again.response) == ("applied", "replayed")
        assert again.result["entry_id"] == first.result["entry_id"]

    def test_conflict_is_detected_over_the_redacted_form(self, tokenizing: Journal) -> None:
        tokenizing.handle(post("order-42", call_id="c1", description="alice@example.com"))
        changed = tokenizing.handle(post("order-42", call_id="c2", description="bob@example.com"))
        assert changed.response == "conflict"

    def test_invalid_input_envelope_carries_no_raw_values(self, tokenizing: Journal) -> None:
        tokenizing.handle(
            {
                "tool": "post",
                "call_id": "call-7",
                "key": "order-42",
                "arguments": {"note": "alice@example.com", "SO-1": 1},
            }
        )
        stored = everything_stored(tokenizing.path)
        for secret in ("alice@example.com", "SO-1", "call-7", "order-42"):
            assert secret not in stored, secret
        (inv,) = table(tokenizing.path, "invocations")
        assert TOKEN_PATTERN.match(inv[8])  # tokenized call_id
        envelope = json.loads(table(tokenizing.path, "events")[0][3])
        assert REDACTION_PATTERN.match(envelope["payload"])
        assert len(envelope["input_digest"]) == 64

    def test_core_error_messages_echo_only_tokens(self, tokenizing: Journal) -> None:
        r = tokenizing.handle(
            {
                "tool": "advance",
                "call_id": "c",
                "key": "k",
                "arguments": {"transaction_id": "txn-alice", "event": "authorize"},
            }
        )
        assert r.response == "rejected" and r.error_type == "UnknownTransactionError"
        assert "txn-alice" not in (r.error_message or "")
        assert TK.tokenize("txn-alice") in (r.error_message or "")

    def test_definition_account_names_are_redacted(self, tokenizing: Journal) -> None:
        (d,) = table(tokenizing.path, "definition")
        names = [a["name"] for a in json.loads(d[10])]
        assert all(n == "" or REDACTION_PATTERN.match(n) for n in names)
        assert d[6] == "acme" and d[7] == "v1"

    def test_invalid_identifier_inside_arguments_is_still_an_admission_failure(
        self, tokenizing: Journal
    ) -> None:
        r = tokenizing.handle(
            {
                "tool": "open_transaction",
                "call_id": "c",
                "key": "k",
                "arguments": {"transaction_id": "a\nb", "amount": {"amount": 1, "currency": "USD"}},
            }
        )
        assert (
            r.response == "invalid"
            and r.error_message == "invalid_identifier at arguments.transaction_id"
        )

    def test_message_content_is_redacted(self, tokenizing: Journal) -> None:
        tokenizing.record_message("user", "my card is 4111 1111")
        assert "4111" not in everything_stored(tokenizing.path)


class TestReplayAndKeyBinding:
    def test_reopen_for_admission_requires_the_same_key(self, tokenizing: Journal) -> None:
        tokenizing.handle(post("order-42", call_id="c1", description="alice@example.com"))
        head, path = tokenizing.ledger.head, tokenizing.path
        tokenizing.close()
        same = Journal.open(
            path,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(start=5),
            admitter=TokenizingAdmitter(TK),
        )
        assert same.ledger.head == head
        assert (
            same.handle(post("order-42", call_id="c2", description="alice@example.com")).response
            == "replayed"
        )
        same.close()
        with pytest.raises(ConfigurationError, match="tokens"):
            Journal.open(
                path, clock=SteppingClock(EPOCH), ids=SequentialIds(), admitter=IdentityAdmitter()
            )
        other_key = TokenizingAdmitter(Tokenizer(bytes(32), domain="acme", key_version="v2"))
        with pytest.raises(ConfigurationError, match="tokens"):
            Journal.open(path, clock=SteppingClock(EPOCH), ids=SequentialIds(), admitter=other_key)

    def test_a_different_key_with_the_same_version_label_is_refused_at_open(
        self, tmp_path: Path
    ) -> None:
        """The journal holds no key material, but it holds a keyed check value, so a wrong
        key under the right label is detected before it can fork the identifier space."""
        path = str(tmp_path / "k.journal")
        j = Journal.create(
            path,
            CHART,
            clock=SteppingClock(EPOCH),
            ids=SequentialIds(),
            admitter=TokenizingAdmitter(TK),
        )
        j.handle(post("order-42", call_id="c1"))
        j.close()
        wrong = TokenizingAdmitter(Tokenizer(bytes(32), domain="acme", key_version="v1"))
        with pytest.raises(ConfigurationError, match="token check"):
            Journal.open(
                path, clock=SteppingClock(EPOCH), ids=SequentialIds(start=5), admitter=wrong
            )
        (d,) = table(path, "definition")
        assert d[8] == TK.key_check() and len(d[8]) == 43  # not the key: a keyed check value

    def test_unresolved_entry_reference_never_reaches_a_row(self, tokenizing: Journal) -> None:
        r = tokenizing.handle(
            {
                "tool": "reverse",
                "call_id": "c",
                "key": "k",
                "arguments": {"entry_id": "jane.doe@example.com 4111-1111"},
            }
        )
        assert r.response == "invalid" and r.error_message == "unknown_entry at arguments.entry_id"
        assert "jane.doe" not in everything_stored(tokenizing.path)
        applied = tokenizing.handle(post("k2", call_id="c2"))
        ok = tokenizing.handle(
            {
                "tool": "reverse",
                "call_id": "c3",
                "key": "k3",
                "arguments": {"entry_id": applied.result["entry_id"], "description": "why"},
            }
        )
        assert ok.response == "applied"

    def test_tag_keys_are_redacted_too(self, tokenizing: Journal) -> None:
        tokenizing.handle(
            post("k", call_id="c", tags={"card 4111111111111111": "x", "ssn": "123-45-6789"})
        )
        stored = everything_stored(tokenizing.path)
        assert "4111" not in stored and "123-45" not in stored and '"ssn"' not in stored


def test_create_warns_on_sensitive_looking_account_ids(tmp_path: Path) -> None:
    chart = ChartOfAccounts(
        [
            Account("alice@example.com", AccountType.LIABILITY, USD),
            Account("revenue", AccountType.REVENUE, USD),
        ]
    )
    with pytest.warns(UserWarning, match="looks like an email"):
        j = Journal.create(
            str(tmp_path / "w.journal"), chart, clock=SteppingClock(EPOCH), ids=SequentialIds()
        )
    j.close()
