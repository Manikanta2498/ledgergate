"""Keyed tokenization and fail-closed redaction, held to docs/spec/identifiers-and-redaction.md."""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ledgergate.codec import REDACTION_PATTERN, TOKEN_PATTERN, Tokenizer
from ledgergate.ledger import (
    USD,
    Advance,
    EntryDraft,
    InvalidIdentifierError,
    Money,
    OpenTransaction,
    Post,
    Refund,
    Reverse,
    TransactionEvent,
    command_fingerprint,
    credit,
    debit,
)

KEY = bytes(range(32))
TK = Tokenizer(KEY, domain="acme", key_version="v1")


class TestConstruction:
    def test_short_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least 16 bytes"):
            Tokenizer(b"short", domain="acme", key_version="v1")

    @pytest.mark.parametrize("domain", ["", "-acme", "acme-", "Acme", "a" * 33, "ac me"])
    def test_bad_domains_are_refused(self, domain: str) -> None:
        with pytest.raises(ValueError, match="domain"):
            Tokenizer(KEY, domain=domain, key_version="v1")

    @pytest.mark.parametrize("domain", ["a", "acme", "a-b", "0" * 32])
    def test_good_domains(self, domain: str) -> None:
        assert Tokenizer(KEY, domain=domain, key_version="v1").domain == domain

    def test_repr_never_shows_the_key(self) -> None:
        assert "\\x00" not in repr(TK) and "00" not in repr(TK)
        assert "acme" in repr(TK)


class TestTokens:
    def test_format_and_length(self) -> None:
        token = TK.tokenize("order-42")
        assert TOKEN_PATTERN.match(token)
        assert token.startswith("tk1_acme_") and 49 <= len(token) <= 80
        assert re.fullmatch(r"[A-Za-z0-9_-]+", token)

    def test_deterministic_under_one_key_and_distinct_across_keys_and_domains(self) -> None:
        assert TK.tokenize("x") == Tokenizer(KEY, domain="acme", key_version="v1").tokenize("x")
        assert TK.tokenize("x") != TK.tokenize("y")
        assert TK.tokenize("x") != Tokenizer(KEY, domain="other", key_version="v1").tokenize("x")
        assert TK.tokenize("x") != Tokenizer(bytes(32), domain="acme", key_version="v1").tokenize(
            "x"
        )

    @pytest.mark.parametrize("raw", ["", "two\nlines", " padded", "x" * 257])
    def test_invalid_identifiers_are_refused_before_tokenizing(self, raw: str) -> None:
        with pytest.raises(InvalidIdentifierError):
            TK.tokenize(raw)

    def test_a_token_is_itself_a_valid_identifier_and_tokenizes_again(self) -> None:
        token = TK.tokenize("x")
        assert TK.tokenize(token) != token  # no fixed points, no accidental idempotence

    @given(st.from_regex(r"\A[^\s\x00-\x1f\x7f\x85\u2028\u2029]{1,200}\Z"))
    def test_every_valid_identifier_yields_a_well_formed_token(self, raw: str) -> None:
        if raw != raw.strip():
            return
        assert TOKEN_PATTERN.match(TK.tokenize(raw))


class TestRedaction:
    def test_format_determinism_and_empty(self) -> None:
        assert TK.redact("") == ""
        a, b = TK.redact("Alice's refund"), TK.redact("Alice's refund")
        assert a == b and REDACTION_PATTERN.match(a)
        assert TK.redact("Bob's refund") != a
        assert TK.redact("x") != TK.tokenize("x")  # text and identifier domains are separate

    def test_redact_json_is_fail_closed_on_keys_strings_and_numbers(self) -> None:
        doc = {
            "customer": "alice@example.com",
            "bob@example.com": {"balance": 5, "card": 4111111111111111},
            "ok": True,
            "none": None,
            "list": ["a", 1, None, 2.5],
        }
        out = TK.redact_json(doc)
        assert set(out) == {TK.redact(k) for k in doc}  # keys are caller text too
        inner = out[TK.redact("bob@example.com")]
        assert inner[TK.redact("card")] == TK.redact("4111111111111111")
        assert out[TK.redact("ok")] is True and out[TK.redact("none")] is None
        assert out[TK.redact("list")][2] is None
        assert all(REDACTION_PATTERN.match(str(v)) for v in out[TK.redact("list")] if v is not None)
        text = str(out)
        assert (
            "alice" not in text and "bob" not in text and "4111" not in text and "2.5" not in text
        )

    def test_looks_sensitive_is_conservative(self) -> None:
        from ledgergate.codec import looks_sensitive

        assert looks_sensitive("alice@example.com") and looks_sensitive("4111 1111 1111 1111")
        assert looks_sensitive("+1 555 123 4567") and not looks_sensitive("cash")
        assert not looks_sensitive("acct-2026-01")

    def test_digest_input_is_keyed_and_canonical(self) -> None:
        a = TK.digest_input({"b": 1, "a": "x"})
        assert a == TK.digest_input({"a": "x", "b": 1}) and len(a) == 64
        assert a != Tokenizer(bytes(32), domain="acme", key_version="v1").digest_input(
            {"b": 1, "a": "x"}
        )


class TestCommands:
    def _sale(self) -> EntryDraft:
        return EntryDraft.of(
            debit("cash", Money(5, USD)),
            credit("revenue", Money(5, USD)),
            description="cust@example.com",
            order="SO-1",
        )

    def test_every_command_kind_is_transformed_by_class(self) -> None:
        for command in (
            Post("k", self._sale()),
            Reverse("k", "e-000001", "why"),
            OpenTransaction("k", "txn-alice", Money(5, USD)),
            Advance("k", "txn-alice", TransactionEvent.SETTLE, self._sale()),
            Refund("k", "txn-alice", Money(1, USD), self._sale()),
        ):
            out = TK.command(command)
            assert out.key == TK.tokenize("k")
            text = repr(out)
            assert "cust@example.com" not in text and "SO-1" not in text and "txn-alice" not in text
            if not isinstance(out, Reverse | OpenTransaction):
                assert "cash" in text  # accounts stay in the clear
            if isinstance(out, Reverse):
                assert out.entry_id == "e-000001"  # class 4: a reference, never tokenized
            if isinstance(out, OpenTransaction):
                assert out.amount == Money(5, USD) and out.transaction_id == TK.tokenize(
                    "txn-alice"
                )

    def test_transform_is_deterministic_so_fingerprints_agree_across_runs(self) -> None:
        c = Advance("k", "txn", TransactionEvent.SETTLE, self._sale())
        assert command_fingerprint(TK.command(c)) == command_fingerprint(
            Tokenizer(KEY, domain="acme", key_version="v1").command(c)
        )
        assert command_fingerprint(TK.command(c)) != command_fingerprint(c)

    @pytest.mark.parametrize(
        "command",
        [
            Post(
                "k",
                EntryDraft.of(
                    debit("cash", Money(5, USD)),
                    credit("revenue", Money(5, USD)),
                    description="d",
                    o="1",
                ),
            ),
            Reverse("k", "e-000001", "why"),
            OpenTransaction("k", "txn-alice", Money(5, USD)),
            Advance(
                "k",
                "txn-alice",
                TransactionEvent.SETTLE,
                EntryDraft.of(
                    debit("cash", Money(5, USD)),
                    credit("revenue", Money(5, USD)),
                    description="d",
                    o="1",
                ),
            ),
            Refund(
                "k",
                "txn-alice",
                Money(1, USD),
                EntryDraft.of(
                    debit("revenue", Money(1, USD)), credit("cash", Money(1, USD)), note="n"
                ),
            ),
        ],
        ids=lambda c: type(c).__name__,
    )
    def test_arguments_document_transform_matches_command_transform(self, command: object) -> None:
        """The journal transforms the JSON document; the recorder transforms the runtime
        command. Both must yield the same stored command, or the two paths would diverge."""
        from ledgergate.codec import decode_command, encode_command
        from ledgergate.ledger import CURRENCIES, Command

        assert isinstance(command, Post | Reverse | OpenTransaction | Advance | Refund)
        cmd: Command = command
        doc = encode_command(cmd)
        arguments = {k: v for k, v in doc.items() if k not in ("kind", "key")}
        via_doc = decode_command(
            {"kind": doc["kind"], "key": TK.tokenize("k"), **TK.arguments(doc["kind"], arguments)},
            CURRENCIES,
        )
        assert via_doc == TK.command(cmd)
        assert command_fingerprint(via_doc) == command_fingerprint(TK.command(cmd))

    def test_arguments_leaves_wrong_types_for_the_codec(self) -> None:
        out = TK.arguments(
            "post", {"draft": {"description": 5, "tags": {"a": 1}}, "transaction_id": 3}
        )
        assert out["draft"]["description"] == 5 and out["transaction_id"] == 3
        assert out["draft"]["tags"] == {TK.redact("a"): 1}  # the key is text; the value is left
