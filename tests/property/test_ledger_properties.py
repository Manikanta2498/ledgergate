"""Ledger-wide invariants under arbitrary command sequences.

These are the claims in the README, stated as properties: the books always balance, the
chain always verifies, replay is byte-identical, and a retried command never applies twice.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from ledgergate.ledger import (
    EPOCH,
    EUR,
    USD,
    Account,
    AccountType,
    ChartOfAccounts,
    Command,
    EntryDraft,
    Ledger,
    LedgerError,
    Money,
    Post,
    Posting,
    Reverse,
    SequentialIds,
    SteppingClock,
    credit,
    debit,
    replay,
)

# A chart where every account allows a negative balance, so any balanced draft is
# postable and the only thing that can reject a Post is a bug.
USD_ACCOUNTS = ("cash", "revenue", "fees", "clearing")
EUR_ACCOUNTS = ("cash:eur", "revenue:eur")
CHART = ChartOfAccounts(
    [
        Account("cash", AccountType.ASSET, USD),
        Account("revenue", AccountType.REVENUE, USD),
        Account("fees", AccountType.EXPENSE, USD),
        Account("clearing", AccountType.LIABILITY, USD),
        Account("cash:eur", AccountType.ASSET, EUR),
        Account("revenue:eur", AccountType.REVENUE, EUR),
    ]
)


@st.composite
def balanced_drafts(draw: st.DrawFn) -> EntryDraft:
    """A balanced draft: N debits and one credit per currency leg, or the reverse."""
    legs: list[Posting] = []
    for accounts, cur in ((USD_ACCOUNTS, USD), (EUR_ACCOUNTS, EUR)):
        if not draw(st.booleans()) and legs:
            continue
        amounts = draw(st.lists(st.integers(1, 10**7), min_size=1, max_size=4))
        debit_accounts = draw(
            st.lists(st.sampled_from(accounts), min_size=len(amounts), max_size=len(amounts))
        )
        credit_account = draw(st.sampled_from(accounts))
        total = sum(amounts)
        lines = [debit(a, Money(m, cur)) for a, m in zip(debit_accounts, amounts, strict=True)]
        lines.append(credit(credit_account, Money(total, cur)))
        if draw(st.booleans()):
            lines = [p.flipped() for p in lines]
        legs.extend(lines)
    return EntryDraft(tuple(legs), description=draw(st.text(max_size=20)))


keys = st.text(alphabet="abcdefghij", min_size=1, max_size=3)


@st.composite
def command_sequences(draw: st.DrawFn) -> list[Command]:
    """Posts, retries of earlier posts, and reversals of earlier entries by sequence."""
    commands: list[Command] = []
    posts = draw(st.lists(st.tuples(keys, balanced_drafts()), min_size=1, max_size=12))
    for key, draft in posts:
        commands.append(Post(key, draft))
        if draw(st.booleans()):
            commands.append(Post(key, draft))  # a retry
        if draw(st.integers(0, 3)) == 0:
            n = draw(st.integers(1, len(commands)))
            commands.append(Reverse(f"rev-{key}-{n}", f"e-{n:06d}"))
    return commands


def run(commands: list[Command]) -> Ledger:
    """Fold commands, tolerating the LedgerErrors a random sequence legitimately raises."""
    ledger = Ledger.empty(CHART)
    clock, ids = SteppingClock(EPOCH), SequentialIds()
    for command in commands:
        try:
            ledger = ledger.execute(command, clock=clock, ids=ids).ledger
        except LedgerError:
            continue
    return ledger


@settings(max_examples=150)
@given(command_sequences())
def test_books_always_balance(commands: list[Command]) -> None:
    ledger = run(commands)
    assert ledger.trial_balance().is_balanced
    for cur, accounts in ((USD, USD_ACCOUNTS), (EUR, EUR_ACCOUNTS)):
        assert sum(ledger.raw_balance(a) for a in accounts) == 0, cur


@settings(max_examples=150)
@given(command_sequences())
def test_chain_always_verifies(commands: list[Command]) -> None:
    ledger = run(commands)
    assert ledger.verify_chain()
    assert [e.sequence for e in ledger.entries] == list(range(1, ledger.sequence + 1))


@settings(max_examples=150)
@given(command_sequences())
def test_replay_is_deterministic(commands: list[Command]) -> None:
    def fold() -> Ledger:
        ledger = Ledger.empty(CHART)
        clock, ids = SteppingClock(EPOCH), SequentialIds()
        for command in commands:
            try:
                ledger = ledger.execute(command, clock=clock, ids=ids).ledger
            except LedgerError:
                continue
        return ledger

    one, two = fold(), fold()
    assert one == two, "whole-ledger equality, not just the head"
    assert one.head == two.head
    assert [e.digest for e in one.entries] == [e.digest for e in two.entries]
    assert one.trial_balance() == two.trial_balance()


@settings(max_examples=150)
@given(command_sequences())
def test_retrying_every_command_changes_nothing(commands: list[Command]) -> None:
    """Idempotency: applying each command twice yields the same ledger as applying it once."""
    clock, ids = SteppingClock(EPOCH), SequentialIds()
    once = Ledger.empty(CHART)
    for command in commands:
        try:
            once = once.execute(command, clock=clock, ids=ids).ledger
        except LedgerError:
            continue
        retried = once.execute(command, clock=clock, ids=ids)
        assert retried.replayed
        assert retried.ledger is once


@settings(max_examples=100)
@given(st.lists(balanced_drafts(), min_size=1, max_size=10))
def test_reversing_everything_returns_to_zero(drafts: list[EntryDraft]) -> None:
    clock, ids = SteppingClock(EPOCH), SequentialIds()
    ledger = Ledger.empty(CHART)
    for i, draft in enumerate(drafts):
        ledger = ledger.execute(Post(f"p{i}", draft), clock=clock, ids=ids).ledger
    for i in range(1, len(drafts) + 1):
        ledger = ledger.execute(Reverse(f"r{i}", f"e-{i:06d}"), clock=clock, ids=ids).ledger
    assert all(ledger.raw_balance(a) == 0 for a in (*USD_ACCOUNTS, *EUR_ACCOUNTS))
    assert ledger.sequence == 2 * len(drafts), "append-only: reversals add, never remove"
    assert ledger.verify_chain()


@given(st.lists(balanced_drafts(), min_size=1, max_size=5))
def test_replay_helper_matches_manual_fold(drafts: list[EntryDraft]) -> None:
    commands: list[Command] = [Post(f"k{i}", d) for i, d in enumerate(drafts)]
    via_helper = replay(CHART, commands, clock=SteppingClock(EPOCH), ids=SequentialIds())
    manual = Ledger.empty(CHART)
    clock, ids = SteppingClock(EPOCH), SequentialIds()
    for c in commands:
        manual = manual.execute(c, clock=clock, ids=ids).ledger
    assert via_helper.head == manual.head
