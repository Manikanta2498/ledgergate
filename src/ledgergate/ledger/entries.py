# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Journal entries: a draft is validated for balance at construction, a posted entry is
immutable and hash-chained to its predecessor.

An :class:`EntryDraft` cannot exist unbalanced. That is a deliberate choice: the invariant
lives in the constructor, so there is no window in which an unbalanced entry is a valid
Python object waiting for someone to remember to check it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from ledgergate.ledger.accounts import Side
from ledgergate.ledger.errors import EmptyEntryError, InvalidAmountError, UnbalancedEntryError
from ledgergate.ledger.money import Currency, Money

GENESIS_HASH = "0" * 64
"""The ``previous_hash`` of the first entry in any ledger."""


def encode(*fields: str) -> str:
    """Length-prefix each field so the concatenation is unambiguous.

    ``encode("a;b", "c") == "3:a;b1:c"`` and can only be produced by those two fields in
    that order. Delimiter-joined strings do not have that property: ``"1;y=2"`` as one tag
    value and ``"1"``, ``"y=2"`` as two collide, and a collision in a canonical form is an
    idempotency bypass. Sequences are encoded as their length followed by their items,
    so a nested structure is unambiguous too.
    """
    return "".join(f"{len(f)}:{f}" for f in fields)


def encode_seq(items: Iterable[str]) -> str:
    items = tuple(items)
    return encode(str(len(items)), *items)


@dataclass(frozen=True, slots=True)
class Posting:
    """One line of an entry: an account, a side, and a strictly positive amount.

    The sign lives in ``side``, never in the amount. A negative debit is a credit, and
    letting both spellings exist is how sign errors hide.
    """

    account_id: str
    side: Side
    money: Money

    def __post_init__(self) -> None:
        if not self.money.is_positive:
            raise InvalidAmountError(
                f"posting to {self.account_id!r} must be strictly positive, got {self.money}"
            )

    @property
    def signed_amount(self) -> int:
        """Debit-positive minor units."""
        return self.side.sign * self.money.amount

    @property
    def currency(self) -> Currency:
        return self.money.currency

    def flipped(self) -> Posting:
        return Posting(self.account_id, self.side.opposite, self.money)

    def canonical(self) -> str:
        # The exponent is part of what the amount means: 100 USD/2 and 100 USD/3 are
        # different requests and must never share a fingerprint or a hash.
        return encode(
            self.account_id,
            self.side.value,
            str(self.money.amount),
            self.currency.code,
            str(self.currency.exponent),
        )


def debit(account_id: str, money: Money) -> Posting:
    return Posting(account_id, Side.DEBIT, money)


def credit(account_id: str, money: Money) -> Posting:
    return Posting(account_id, Side.CREDIT, money)


def net_by_currency(postings: Iterable[Posting]) -> dict[Currency, int]:
    """Net debit-minus-credit per currency. All zeros means balanced."""
    net: dict[Currency, int] = {}
    for posting in postings:
        net[posting.currency] = net.get(posting.currency, 0) + posting.signed_amount
    return net


@dataclass(frozen=True, slots=True)
class EntryDraft:
    """A balanced set of postings that has not been posted yet.

    Construction fails with :class:`UnbalancedEntryError` if any currency's debits do
    not equal its credits, so holding an ``EntryDraft`` is proof of balance.
    """

    postings: tuple[Posting, ...]
    description: str = ""
    tags: tuple[tuple[str, str], ...] = field(default=())

    def __post_init__(self) -> None:
        # Annotations are not enforced at runtime: a caller can pass a list, keep a
        # reference, and append to it after this constructor has certified balance. Copy
        # into tuples *first*, then validate the copies, so what was checked is what is
        # kept. Every element is checked too, so a stray non-Posting cannot ride along.
        postings = tuple(self.postings)
        # Tags are a map, not a list: their order is not part of the request, so the
        # canonical order is by key, here and in every codec, or a round trip would change
        # the fingerprint.
        tags = tuple(sorted((str(k), str(v)) for k, v in self.tags))
        object.__setattr__(self, "postings", postings)
        object.__setattr__(self, "tags", tags)

        if any(not isinstance(p, Posting) for p in postings):
            raise InvalidAmountError("postings must all be Posting instances")
        if len(postings) < 2:
            raise EmptyEntryError()
        imbalance = {c.code: n for c, n in net_by_currency(postings).items() if n != 0}
        if imbalance:
            raise UnbalancedEntryError(imbalance)
        if any(k != k.strip() or not k for k, _ in tags):
            raise InvalidAmountError("tag keys must be non-empty and trimmed")
        if len({k for k, _ in tags}) != len(tags):
            raise InvalidAmountError("tag keys must be unique")

    @classmethod
    def of(cls, *postings: Posting, description: str = "", **tags: str) -> EntryDraft:
        return cls(tuple(postings), description, tuple(sorted(tags.items())))

    @property
    def currencies(self) -> frozenset[Currency]:
        return frozenset(p.currency for p in self.postings)

    @property
    def account_ids(self) -> frozenset[str]:
        return frozenset(p.account_id for p in self.postings)

    @property
    def account_order(self) -> tuple[str, ...]:
        """Distinct account ids in first-appearance order: the deterministic iteration order
        for anything whose *result* depends on which account is examined first."""
        return tuple(dict.fromkeys(p.account_id for p in self.postings))

    def tag(self, key: str) -> str | None:
        return dict(self.tags).get(key)

    def gross(self, currency: Currency) -> Money:
        """Total debited in ``currency``: the amount this entry moves in that currency.

        Because the draft balances, this equals the total credited too. It is what a
        lifecycle event compares against: a settlement of 100.00 must move 100.00.
        """
        return Money(
            sum(
                p.money.amount
                for p in self.postings
                if p.side is Side.DEBIT and p.currency == currency
            ),
            currency,
        )

    def moved(self, currency: Currency) -> Money:
        """The amount that actually changes hands in ``currency``: the sum of each account's
        positive net movement. A debit and a credit of the same amount to one account net to
        zero and move nothing, however large the gross; this is what a lifecycle event must
        equal, so a self-cancelling entry cannot mark a transaction settled."""
        net: dict[str, int] = {}
        for p in self.postings:
            if p.currency == currency:
                net[p.account_id] = net.get(p.account_id, 0) + p.signed_amount
        return Money(sum(n for n in net.values() if n > 0), currency)

    def reversed(self, description: str = "") -> EntryDraft:
        return EntryDraft(
            tuple(p.flipped() for p in self.postings),
            description or f"reversal: {self.description}",
            self.tags,
        )

    def canonical(self) -> str:
        """A stable textual form used for hashing and idempotency fingerprints.

        Postings keep their order: two drafts with the same lines in a different order
        are different requests, and pretending otherwise would let a retry collide.
        """
        return encode(
            "draft",
            self.description,
            encode_seq(encode(k, v) for k, v in self.tags),
            encode_seq(p.canonical() for p in self.postings),
        )


@dataclass(frozen=True, slots=True)
class Entry:
    """A posted, immutable journal entry with its position in the hash chain."""

    entry_id: str
    sequence: int
    posted_at: datetime
    idempotency_key: str
    draft: EntryDraft
    previous_hash: str
    digest: str
    reverses: str | None = None

    @property
    def postings(self) -> tuple[Posting, ...]:
        return self.draft.postings

    @property
    def description(self) -> str:
        return self.draft.description

    def canonical(self) -> str:
        return _entry_body(
            entry_id=self.entry_id,
            sequence=self.sequence,
            posted_at=self.posted_at,
            idempotency_key=self.idempotency_key,
            draft=self.draft,
            previous_hash=self.previous_hash,
            reverses=self.reverses,
        )

    @staticmethod
    def compute_digest(
        *,
        entry_id: str,
        sequence: int,
        posted_at: datetime,
        idempotency_key: str,
        draft: EntryDraft,
        previous_hash: str,
        reverses: str | None,
    ) -> str:
        """SHA-256 over the canonical form. Pure function of its inputs."""
        body = _entry_body(
            entry_id=entry_id,
            sequence=sequence,
            posted_at=posted_at,
            idempotency_key=idempotency_key,
            draft=draft,
            previous_hash=previous_hash,
            reverses=reverses,
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def recomputed_digest(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()


def _entry_body(
    *,
    entry_id: str,
    sequence: int,
    posted_at: datetime,
    idempotency_key: str,
    draft: EntryDraft,
    previous_hash: str,
    reverses: str | None,
) -> str:
    """The one canonical serialization both `compute_digest` and `canonical` use."""
    return encode(
        "entry",
        entry_id,
        str(sequence),
        posted_at.isoformat(),
        idempotency_key,
        "" if reverses is None else encode(reverses),
        previous_hash,
        draft.canonical(),
    )


def fingerprint(kind: str, payload: Mapping[str, str]) -> str:
    """A stable fingerprint for an idempotent request, used to detect key reuse."""
    body = encode(kind, encode_seq(encode(k, payload[k]) for k in sorted(payload)))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
