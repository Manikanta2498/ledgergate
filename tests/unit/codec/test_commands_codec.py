"""The one command codec: shape, errors, and the fingerprint round-trip invariant."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ledgergate.codec import CodecError, decode_command, encode_command
from ledgergate.ledger import (
    CURRENCIES,
    EUR,
    USD,
    Advance,
    Command,
    EntryDraft,
    Money,
    OpenTransaction,
    Post,
    Refund,
    Reverse,
    TransactionEvent,
    UnbalancedEntryError,
    command_fingerprint,
    credit,
    debit,
)

REG = CURRENCIES
E = TransactionEvent


def sale(n: int = 5) -> EntryDraft:
    return EntryDraft.of(debit("cash", Money(n, USD)), credit("revenue", Money(n, USD)))


COMMANDS: list[Command] = [
    Post(
        "k",
        EntryDraft.of(
            debit("cash", Money(5, USD)), credit("rev", Money(5, USD)), description="d", a="1"
        ),
    ),
    Reverse("k", "e-1", "why"),
    Reverse("k", "e-1"),
    OpenTransaction("k", "t", Money(100, USD)),
    OpenTransaction("k", "t", Money(0, USD)),
    Advance("k", "t", E.AUTHORIZE),
    Advance("k", "t", E.SETTLE, sale(1)),
    Refund("k", "t", Money(1, USD)),
    Refund(
        "k",
        "t",
        Money(-3, EUR),
        EntryDraft.of(debit("rev", Money(1, USD)), credit("cash", Money(1, USD))),
    ),
]


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: type(c).__name__)
def test_round_trip_preserves_command_and_fingerprint(command: Command) -> None:
    doc = encode_command(command)
    back = decode_command(doc, REG)
    assert back == command
    assert command_fingerprint(back) == command_fingerprint(command)


def test_encoded_shape_is_the_v1_command_object() -> None:
    doc = encode_command(Post("k", sale()))
    assert doc["kind"] == "post" and doc["key"] == "k"
    assert doc["draft"]["postings"][0] == {
        "account": "cash",
        "side": "debit",
        "money": {"amount": 5, "currency": "USD"},
    }
    assert "description" not in doc["draft"] and "tags" not in doc["draft"]


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ("nope", "must be an object"),
        ({"key": "k"}, "missing 'kind'"),
        ({"kind": "post"}, "missing 'key'"),
        ({"kind": "teleport", "key": "k"}, "unknown kind"),
        ({"kind": "post", "key": "k"}, "draft: must be an object"),
        ({"kind": "post", "key": "k", "draft": {"postings": []}, "extra": 1}, "unknown fields"),
        ({"kind": "post", "key": "k", "draft": {"postings": "x"}}, "expected list"),
        (
            {
                "kind": "open_transaction",
                "key": "k",
                "transaction_id": "t",
                "amount": {"amount": "5", "currency": "USD"},
            },
            "expected int",
        ),
        (
            {
                "kind": "open_transaction",
                "key": "k",
                "transaction_id": "t",
                "amount": {"amount": True, "currency": "USD"},
            },
            "expected int",
        ),
        (
            {
                "kind": "open_transaction",
                "key": "k",
                "transaction_id": "t",
                "amount": {"amount": 5, "currency": "ZZZ"},
            },
            "not in the registry",
        ),
        ({"kind": "advance", "key": "k", "transaction_id": "t", "event": "teleport"}, "event"),
        (
            {
                "kind": "post",
                "key": "k",
                "draft": {
                    "postings": [
                        {
                            "account": "a",
                            "side": "sideways",
                            "money": {"amount": 1, "currency": "USD"},
                        }
                    ]
                },
            },
            "side",
        ),
    ],
)
def test_malformed_documents_are_codec_errors_naming_the_location(
    doc: object, message: str
) -> None:
    with pytest.raises(CodecError, match=message):
        decode_command(doc, REG)


def test_structural_decoding_lets_the_core_raise_its_own_errors() -> None:
    doc = {
        "kind": "post",
        "key": "k",
        "draft": {
            "postings": [
                {"account": "cash", "side": "debit", "money": {"amount": 2, "currency": "USD"}},
                {"account": "revenue", "side": "credit", "money": {"amount": 1, "currency": "USD"}},
            ]
        },
    }
    with pytest.raises(UnbalancedEntryError):
        decode_command(doc, REG)


@given(
    amount=st.integers(1, 10**12),
    description=st.text(max_size=40),
    tags=st.dictionaries(
        st.from_regex(r"\A[a-z][a-z0-9_]{0,7}\Z"), st.text(max_size=8), max_size=4
    ),
)
def test_property_round_trip(amount: int, description: str, tags: dict[str, str]) -> None:
    draft = EntryDraft.of(
        debit("cash", Money(amount, USD)),
        credit("revenue", Money(amount, USD)),
        description=description,
        **tags,
    )
    command = Post("k", draft)
    assert decode_command(encode_command(command), REG) == command


@pytest.mark.parametrize(
    "doc",
    [
        {"kind": "post", "key": "k", "draft": {"postings": [], "SECRET-FIELD": 1}},
        {
            "kind": "post",
            "key": "k",
            "draft": {"postings": [{"account": "a", "side": "SECRET", "money": {}}]},
        },
        {"kind": "advance", "key": "k", "transaction_id": "t", "event": "SECRET-EVENT"},
        {"kind": "SECRET-KIND", "key": "k"},
        {
            "kind": "open_transaction",
            "key": "k",
            "transaction_id": "t",
            "amount": {"amount": 1, "currency": "SECRET"},
        },
        {
            "kind": "refund",
            "key": "k",
            "transaction_id": "t",
            "money": {"amount": 1, "currency": "USD"},
            "SECRET": 2,
        },
    ],
)
def test_codec_error_location_never_contains_document_values(doc: dict[str, object]) -> None:
    """``CodecError.where`` is what admission records; it must be built from literals only."""
    with pytest.raises(CodecError) as exc:
        decode_command(doc, REG)
    assert "SECRET" not in exc.value.where
