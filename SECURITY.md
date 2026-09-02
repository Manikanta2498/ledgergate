# Security

LedgerGate is pre-alpha and has no production deployments. That does not make a
correctness bug in a ledger unimportant, so please report anyway.

## Reporting

Email **yvsaimanikanta+ledgergate@gmail.com** with a description and, if you can, a
reproduction. Please do not open a public issue for anything that could let a caller move
money twice, move money that does not balance, bypass an idempotency key, or pass an
invalid state through the lifecycle machine. Those are the failures this project exists to
prevent, and they get fixed first.

You will get an acknowledgement within a few days. Fixes ship with a regression test that
reproduces the report.

## What counts

Anything that lets the following happen is in scope:

- A command applies twice under the same idempotency key, or a different request is
  accepted as a replay.
- An `EntryDraft` exists, or is posted, unbalanced.
- `verify_chain()` returns `True` for a ledger whose entries, balances or indexes have
  been altered.
- A `Transaction` reaches a status its refunded total contradicts, or takes a transition
  the table does not permit.
- A monetary lifecycle event applies without a journal entry that moves the stated amount.
- A "frozen" structure can be mutated after construction.
- The ledger core reaches a wall clock, a random source or a `float` in a way the
  determinism gate does not catch.

Dynamic evasion of the static determinism gate (`getattr`, `importlib`, `exec`) is
documented as out of scope for that gate; it is not a vulnerability in the ledger.

## Supply chain

CI pins every GitHub Action by commit SHA, installs `gitleaks` from a checksum-verified
release, scans the full git history for secrets on every run, and audits dependencies
with `pip-audit`. Dependencies are locked with `uv.lock` and installed with `--locked`.
