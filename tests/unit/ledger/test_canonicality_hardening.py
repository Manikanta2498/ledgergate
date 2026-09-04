"""Canonicality defects found by whole-project review: currency exponent excluded from
fingerprints and hashes, hash-seed-dependent rejection messages, tag order changed by the
codec, and a replay returning the transaction's state now rather than then."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ledgergate.codec import decode_command, encode_command
from ledgergate.ledger import (
    CURRENCIES,
    EPOCH,
    USD,
    Account,
    AccountType,
    Advance,
    ChartOfAccounts,
    Currency,
    EntryDraft,
    InsufficientFundsError,
    Ledger,
    Money,
    OpenTransaction,
    Post,
    Posting,
    SequentialIds,
    Side,
    SteppingClock,
    TransactionEvent,
    TransactionStatus,
    command_fingerprint,
)

CHART = ChartOfAccounts(
    [
        Account("a", AccountType.ASSET, USD, allow_negative=False),
        Account("b", AccountType.ASSET, USD, allow_negative=False),
        Account("x", AccountType.LIABILITY, USD),
    ]
)


def test_currency_exponent_is_part_of_every_canonical_form() -> None:
    usd3 = Currency("USD", 3)
    c2 = OpenTransaction("k", "t", Money(100, USD))
    c3 = OpenTransaction("k", "t", Money(100, usd3))
    assert command_fingerprint(c2) != command_fingerprint(c3)
    p2 = Posting("a", Side.DEBIT, Money(100, USD))
    p3 = Posting("a", Side.DEBIT, Money(100, usd3))
    assert p2.canonical() != p3.canonical()


def test_tag_order_is_canonical_so_the_codec_round_trip_keeps_the_fingerprint() -> None:
    draft = EntryDraft(
        (Posting("a", Side.DEBIT, Money(1, USD)), Posting("x", Side.CREDIT, Money(1, USD))),
        tags=(("z", "1"), ("a", "2")),
    )
    assert draft.tags == (("a", "2"), ("z", "1"))
    post = Post("k", draft)
    back = decode_command(encode_command(post), CURRENCIES)
    assert command_fingerprint(back) == command_fingerprint(post)


def test_overdraft_reports_the_first_offending_account_in_posting_order() -> None:
    ledger = Ledger.empty(CHART)
    draft = EntryDraft(
        (
            Posting("b", Side.CREDIT, Money(5, USD)),
            Posting("a", Side.CREDIT, Money(5, USD)),
            Posting("x", Side.DEBIT, Money(10, USD)),
        )
    )
    with pytest.raises(InsufficientFundsError, match="'b'"):
        ledger.execute(Post("k", draft), clock=SteppingClock(EPOCH), ids=SequentialIds())


_SEED_SCRIPT = """
from ledgergate.ledger import *
chart = ChartOfAccounts([
    Account("a", AccountType.ASSET, USD, allow_negative=False),
    Account("b", AccountType.ASSET, USD, allow_negative=False),
    Account("x", AccountType.LIABILITY, USD)])
draft = EntryDraft((
    Posting("a", Side.CREDIT, Money(5, USD)),
    Posting("b", Side.CREDIT, Money(5, USD)),
    Posting("x", Side.DEBIT, Money(10, USD))))
try:
    Ledger.empty(chart).execute(Post("k", draft), clock=SteppingClock(EPOCH), ids=SequentialIds())
except LedgerError as exc:
    print(exc)
"""


def test_rejection_messages_do_not_depend_on_the_hash_seed() -> None:
    outputs = set()
    for seed in ("1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", _SEED_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parents[3],
        )
        outputs.add(out.stdout)
    assert len(outputs) == 1 and "'a'" in outputs.pop()


def test_core_replay_returns_the_transaction_as_it_was_then() -> None:
    ledger = Ledger.empty(CHART)
    clock, ids = SteppingClock(EPOCH), SequentialIds()
    first = ledger.execute(OpenTransaction("open", "t", Money(10, USD)), clock=clock, ids=ids)
    assert first.transaction is not None and first.transaction.status is TransactionStatus.PENDING
    advanced = first.ledger.execute(
        Advance("auth", "t", TransactionEvent.AUTHORIZE), clock=clock, ids=ids
    )
    assert advanced.transaction is not None
    assert advanced.transaction.status is TransactionStatus.AUTHORIZED
    retry = advanced.ledger.execute(
        OpenTransaction("open", "t", Money(10, USD)), clock=clock, ids=ids
    )
    assert retry.replayed and retry.transaction is not None
    assert retry.transaction.status is TransactionStatus.PENDING  # then, not now


class TestTypeGatesAndMovement:
    def test_constructors_enforce_exact_runtime_types(self) -> None:
        from fractions import Fraction

        from ledgergate.ledger import InvalidAmountError

        with pytest.raises(InvalidAmountError):
            Currency("ZZZ", 2.5)  # type: ignore[arg-type]
        with pytest.raises(InvalidAmountError):
            Currency("USD\n", 2)
        with pytest.raises(InvalidAmountError):
            Account("a", AccountType.ASSET, USD, allow_negative="false")  # type: ignore[arg-type]
        with pytest.raises(InvalidAmountError):
            Money(100, USD).scale(0.005)  # type: ignore[arg-type]
        assert Money(100, USD).scale(Fraction(5, 1000)).amount == 0  # half-even, exact

    def test_self_cancelling_entry_moves_nothing(self) -> None:
        from ledgergate.ledger import EntryAmountMismatchError

        chart = ChartOfAccounts(
            [Account("cash", AccountType.ASSET, USD), Account("rev", AccountType.REVENUE, USD)]
        )
        clock, ids = SteppingClock(EPOCH), SequentialIds()
        led = Ledger.empty(chart)
        led = led.execute(OpenTransaction("o", "t", Money(100, USD)), clock=clock, ids=ids).ledger
        led = led.execute(
            Advance("a", "t", TransactionEvent.AUTHORIZE), clock=clock, ids=ids
        ).ledger
        nowhere = EntryDraft(
            (
                Posting("cash", Side.DEBIT, Money(100, USD)),
                Posting("cash", Side.CREDIT, Money(100, USD)),
            )
        )
        assert nowhere.gross(USD).amount == 100 and nowhere.moved(USD).amount == 0
        with pytest.raises(EntryAmountMismatchError):
            led.execute(Advance("s", "t", TransactionEvent.SETTLE, nowhere), clock=clock, ids=ids)

    def test_fx_legs_must_be_distinct(self) -> None:
        from fractions import Fraction

        from ledgergate.ledger import EUR, InvalidAmountError, StaticRates
        from ledgergate.ledger.fx import conversion_entry

        rates = StaticRates({(USD, EUR): Fraction(9, 10)})
        with pytest.raises(InvalidAmountError, match="distinct"):
            conversion_entry(
                Money(100, USD),
                EUR,
                rates,
                source_account="cash",
                destination_account="eur",
                source_clearing="cash",
                destination_clearing="eur",
            )

    def test_transitions_are_frozen(self) -> None:
        from typing import Any

        from ledgergate.ledger.lifecycle import TRANSITIONS

        table: Any = TRANSITIONS
        with pytest.raises(TypeError):
            table[(TransactionStatus.PENDING, TransactionEvent.REFUND)] = TransactionStatus.REFUNDED

    def test_canonical_text_rejects_lone_surrogates(self) -> None:
        from ledgergate.codec import JcsError, canonical_text

        with pytest.raises(JcsError):
            canonical_text("\ud800")
