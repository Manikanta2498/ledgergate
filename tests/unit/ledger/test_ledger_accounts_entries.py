"""Accounts, the chart, and entry construction invariants."""

from __future__ import annotations

import pytest

from ledgergate.ledger import (
    EUR,
    USD,
    Account,
    AccountType,
    ChartOfAccounts,
    DuplicateAccountError,
    EmptyEntryError,
    EntryDraft,
    InvalidAmountError,
    InvalidIdentifierError,
    Money,
    Posting,
    Side,
    UnbalancedEntryError,
    UnknownAccountError,
    credit,
    debit,
    net_by_currency,
)
from ledgergate.ledger.entries import fingerprint


class TestSides:
    def test_opposite_and_sign(self) -> None:
        assert Side.DEBIT.opposite is Side.CREDIT
        assert Side.CREDIT.opposite is Side.DEBIT
        assert Side.DEBIT.sign == 1 and Side.CREDIT.sign == -1

    @pytest.mark.parametrize(
        ("kind", "side"),
        [
            (AccountType.ASSET, Side.DEBIT),
            (AccountType.EXPENSE, Side.DEBIT),
            (AccountType.LIABILITY, Side.CREDIT),
            (AccountType.EQUITY, Side.CREDIT),
            (AccountType.REVENUE, Side.CREDIT),
        ],
    )
    def test_normal_side(self, kind: AccountType, side: Side) -> None:
        assert kind.normal_side is side


class TestChart:
    def test_lookup_and_iteration(self, chart: ChartOfAccounts) -> None:
        assert chart["cash"].kind is AccountType.ASSET
        assert "cash" in chart
        assert len(chart) == 8
        assert next(iter(chart)) == "cash"
        assert "ChartOfAccounts" in repr(chart)

    def test_unknown(self, chart: ChartOfAccounts) -> None:
        with pytest.raises(UnknownAccountError) as exc:
            chart["nope"]
        assert exc.value.account_id == "nope"

    def test_duplicate(self) -> None:
        a = Account("x", AccountType.ASSET, USD)
        with pytest.raises(DuplicateAccountError):
            ChartOfAccounts([a, a])

    def test_of_type(self, chart: ChartOfAccounts) -> None:
        assert {a.account_id for a in chart.of_type(AccountType.LIABILITY)} == {"wallet:alice"}

    @pytest.mark.parametrize("bad", ["", " cash", "cash "])
    def test_account_id_must_be_trimmed_and_nonempty(self, bad: str) -> None:
        with pytest.raises(InvalidIdentifierError):
            Account(bad, AccountType.ASSET, USD)


class TestPosting:
    def test_signed_amount(self) -> None:
        assert debit("cash", Money(5, USD)).signed_amount == 5
        assert credit("cash", Money(5, USD)).signed_amount == -5

    def test_must_be_positive(self) -> None:
        with pytest.raises(InvalidAmountError):
            Posting("cash", Side.DEBIT, Money(0, USD))
        with pytest.raises(InvalidAmountError):
            Posting("cash", Side.DEBIT, Money(-1, USD))

    def test_flipped(self) -> None:
        p = debit("cash", Money(5, USD))
        assert p.flipped() == credit("cash", Money(5, USD))
        assert p.currency is USD


class TestEntryDraft:
    def test_balanced_constructs(self) -> None:
        d = EntryDraft.of(debit("cash", Money(100, USD)), credit("revenue", Money(100, USD)))
        assert d.currencies == {USD}
        assert d.account_ids == {"cash", "revenue"}

    def test_unbalanced_rejected_with_detail(self) -> None:
        with pytest.raises(UnbalancedEntryError) as exc:
            EntryDraft.of(debit("cash", Money(100, USD)), credit("revenue", Money(99, USD)))
        assert exc.value.imbalance == {"USD": 1}
        assert "USD: +1" in str(exc.value)

    def test_balance_is_per_currency(self) -> None:
        # Balanced in total minor units but not per currency: must fail.
        with pytest.raises(UnbalancedEntryError) as exc:
            EntryDraft.of(debit("cash", Money(100, USD)), credit("cash:eur", Money(100, EUR)))
        assert exc.value.imbalance == {"USD": 100, "EUR": -100}

    def test_multi_currency_balanced_ok(self) -> None:
        d = EntryDraft.of(
            debit("cash", Money(100, USD)),
            credit("fx:usd", Money(100, USD)),
            debit("fx:eur", Money(90, EUR)),
            credit("cash:eur", Money(90, EUR)),
        )
        assert d.currencies == {USD, EUR}

    def test_needs_two_postings(self) -> None:
        with pytest.raises(EmptyEntryError):
            EntryDraft(())
        with pytest.raises(EmptyEntryError):
            EntryDraft((debit("cash", Money(1, USD)),))

    def test_tags(self) -> None:
        d = EntryDraft.of(
            debit("cash", Money(1, USD)), credit("revenue", Money(1, USD)), b="2", a="1"
        )
        assert d.tags == (("a", "1"), ("b", "2"))
        assert d.tag("a") == "1" and d.tag("zzz") is None

    def test_bad_tag_key(self) -> None:
        with pytest.raises(InvalidAmountError):
            EntryDraft(
                (debit("cash", Money(1, USD)), credit("revenue", Money(1, USD))),
                tags=((" a", "1"),),
            )

    def test_input_list_is_copied_so_later_mutation_cannot_unbalance(self) -> None:
        """The constructor certifies balance; the caller must not be able to revoke it."""
        lines = [debit("cash", Money(1, USD)), credit("revenue", Money(1, USD))]
        tags = [["k", "v"]]
        draft = EntryDraft(lines, tags=tags)  # type: ignore[arg-type]
        lines.append(debit("cash", Money(5, USD)))
        tags[0][1] = "changed"
        tags.append(["x", "y"])
        assert isinstance(draft.postings, tuple) and len(draft.postings) == 2
        assert draft.tags == (("k", "v"),)
        assert not any(net_by_currency(draft.postings).values())

    def test_non_posting_elements_are_rejected(self) -> None:
        with pytest.raises(InvalidAmountError):
            EntryDraft((debit("cash", Money(1, USD)), "credit revenue 1"))  # type: ignore[arg-type]

    def test_duplicate_tag_keys_are_rejected(self) -> None:
        with pytest.raises(InvalidAmountError, match="unique"):
            EntryDraft(
                (debit("cash", Money(1, USD)), credit("revenue", Money(1, USD))),
                tags=(("k", "1"), ("k", "2")),
            )

    def test_reversed_flips_every_posting(self) -> None:
        d = EntryDraft.of(
            debit("cash", Money(1, USD)), credit("revenue", Money(1, USD)), description="sale"
        )
        r = d.reversed()
        assert r.postings == (credit("cash", Money(1, USD)), debit("revenue", Money(1, USD)))
        assert r.description == "reversal: sale"
        assert d.reversed("undo").description == "undo"

    def test_canonical_is_order_sensitive(self) -> None:
        a = EntryDraft.of(debit("cash", Money(1, USD)), credit("revenue", Money(1, USD)))
        b = EntryDraft.of(credit("revenue", Money(1, USD)), debit("cash", Money(1, USD)))
        assert a.canonical() != b.canonical()
        assert a.canonical() == a.canonical()

    def test_net_by_currency(self) -> None:
        assert net_by_currency([debit("a", Money(3, USD)), credit("b", Money(1, USD))]) == {USD: 2}


def test_fingerprint_is_key_order_independent() -> None:
    assert fingerprint("k", {"a": "1", "b": "2"}) == fingerprint("k", {"b": "2", "a": "1"})
    assert fingerprint("k", {"a": "1"}) != fingerprint("j", {"a": "1"})
