# LedgerGate

**A correctness-enforcing ledger runtime for autonomous agents that move money, plus the
invariant conformance suite that proves an agent respects financial state machines before
deployment.**

> **Status: pre-alpha, milestone M1.** The deterministic ledger core is implemented and
> tested: balanced double-entry postings, idempotent commands, a payment state machine,
> a hash-chained audit trail and hash-identical replay. The agent-facing pieces (trace
> schema, corpus, adapters, CLI) are not built yet. See [Roadmap](#roadmap).

---

## The problem

Fintechs are putting LLM agents in front of refunds, disputes and billing. LLMs are
non-deterministic, and four failure modes follow:

- **Idempotency failures.** A tool call times out, the agent retries without a key, and a
  refund pays out twice.
- **Ledger invariant violations.** A cross-currency conversion fails to balance across
  asset and liability accounts, leaking money silently.
- **Illegal state transitions.** The agent jumps `PENDING` straight to `REFUNDED`,
  skipping `SETTLED`.
- **Behavioral drift.** A prompt tweak or a model swap raises the error rate on edge
  cases, and nothing catches it.

You cannot check any of this with another probabilistic prompt. LLM-as-a-judge cannot tell
you whether debits equal credits. That is an assertion, not an opinion.

## The approach

Deterministic invariants over a versioned execution trace.

```
agent run ──▶ trace (schema v1) ──▶ invariants ──▶ result.json ──▶ md | junit | sarif
                    ▲
       adapters: anthropic | openai | langgraph
```

The interop contract will be `schema/trace/v1.json`, a JSON Schema 2020-12 document, not
our harness. Once it exists, any agent that emits the schema can run the corpus, whatever
framework it uses.

**Only the ledger underneath this pipeline exists today.** The trace schema lands in M2,
the corpus and CLI in M3, the adapters in M4. What is real now is the ledger core below,
and the gates that keep it honest, which run in CI on every pull request and every push
to `main`.

## The ledger core

`ledgergate.ledger` is a pure, immutable double-entry ledger. Every write is a command;
every command is a function of `(state, command, effects)` that returns a new ledger or
raises. Nothing mutates (every mapping is a read-only view), nothing is deleted, and every
effect (clock, ids, FX rates) is injected, so the same commands always produce the same
ledger down to the last hash.

```python
from ledgergate.ledger import *

chart = ChartOfAccounts(
    [
        Account("cash", AccountType.ASSET, USD),
        Account("revenue", AccountType.REVENUE, USD),
    ]
)
clock, ids = FixedClock(EPOCH), SequentialIds()
ledger = Ledger.empty(chart)

# A refund pays out once, no matter how many times the agent retries.
sale = EntryDraft.of(debit("cash", Money(1999, USD)), credit("revenue", Money(1999, USD)))
refund = Refund("refund-order-42", "order-42", Money(1999, USD), sale.reversed())

ledger = ledger.execute(
    OpenTransaction("open-42", "order-42", Money(1999, USD)), clock=clock, ids=ids
).ledger
ledger = ledger.execute(
    Advance("auth-42", "order-42", TransactionEvent.AUTHORIZE), clock=clock, ids=ids
).ledger
ledger = ledger.execute(
    Advance("settle-42", "order-42", TransactionEvent.SETTLE, sale), clock=clock, ids=ids
).ledger

first = ledger.execute(refund, clock=clock, ids=ids)
retry = first.ledger.execute(refund, clock=clock, ids=ids)  # the timeout-and-retry case

assert retry.replayed  # recognised, not re-applied
assert retry.ledger.balance("cash") == Money(0, USD)  # paid out once
assert retry.ledger.transaction("order-42").status is TransactionStatus.REFUNDED
assert retry.ledger.verify_chain()  # every entry hash-linked

# Unbalanced entries cannot be constructed, let alone posted.
EntryDraft.of(debit("cash", Money(100, USD)), credit("revenue", Money(99, USD)))
# -> UnbalancedEntryError: entry does not balance (USD: +1)

# Illegal transitions are rejected by the state machine, not by convention.
Ledger.empty(chart).execute(
    OpenTransaction("o", "t", Money(1, USD)), clock=clock, ids=ids
).ledger.execute(Refund("r", "t", Money(1, USD)), clock=clock, ids=ids)
# -> IllegalTransitionError: transaction 't' in pending cannot accept refund
```

What is in the box:

| Area | What you get |
| :--- | :--- |
| Money | Integer minor units, per-currency exponents, exact `Fraction` rates, seven rounding modes, largest-remainder allocation that always sums, float rejected at construction |
| Entries | `EntryDraft` cannot exist unbalanced (checked per currency at construction), strictly positive postings, sign lives in the side |
| Accounts | Five account types with normal sides, per-account currency, optional no-overdraft rule that fails an over-refund loudly |
| Ledger | Immutable, append-only, reversal by mirror entry, trial balance, per-account history, SHA-256 hash chain; `verify_chain()` recomputes every digest *and* re-derives every balance and index from the entries, so an edited balance fails as surely as an edited entry |
| Idempotency | Every command carries a key; same key + same request replays the original result, same key + different request raises. Fingerprints are SHA-256 over an unambiguous length-prefixed encoding, so no delimiter trick can make two different requests serialize the same |
| Lifecycle | `PENDING -> AUTHORIZED -> SETTLED -> PARTIALLY_REFUNDED -> REFUNDED` plus dispute, cancel, fail; illegal moves raise. `SETTLE` and `REFUND` *require* a journal entry that moves exactly the stated amount in the transaction's currency, posted atomically with the transition; other events must not carry one |
| FX | Balanced four-line conversion through clearing accounts, so cross-currency moves cannot leak |
| Effects | `Clock`, `IdGenerator`, `FxRateSource` Protocols with deterministic reference implementations |
| Replay | `replay(chart, commands, clock=..., ids=...)` reproduces an equal ledger: same entries, same digests, same head hash |

Tested by 250+ tests: unit tests per module, Hypothesis property tests (books always
balance, chain always verifies, replay is deterministic, every retry is a no-op), and a
model-based state machine that drives random event sequences through the lifecycle and
checks status, refund totals, cash and chain integrity after every step.

## Design commitments

These are enforced by CI gates, not by convention:

| Commitment | Enforced by |
| :--- | :--- |
| The ledger core is deterministic | `scripts/check_determinism.py` fails on any `random`/`uuid`/`secrets` import, any call to *or reference to* a wall-clock or uuid function, resolved through import aliases and assignments in any scope, and any reuse of a name bound to one. Deliberately conservative: it may reject safe code, it does not model scopes it could get wrong |
| The dependency graph stays one-way | `import-linter` contracts in `pyproject.toml` |
| The ledger core stays pure | `import-linter` forbidden contract: no `json`, `pathlib`, `sqlite3`, no adapters |
| No accidental network in tests | `pytest --disable-socket` by default |
| The license boundary is unambiguous per file | `scripts/check_licenses.py` requires a matching `SPDX-License-Identifier`, inline or in a `.license` sidecar, on every source and package-data file under `src/ledgergate/`, `corpus/` and `schema/` |
| Secrets stay out of the tree and the history | `gitleaks` on staged changes in the pre-commit hook, then on the full working tree *and* the full git history in CI |
| Money is never a float | `Money` rejects a `float` amount at construction, and `scripts/check_determinism.py` fails on any float literal, `float()` call or `float` annotation in `src/ledgergate/ledger/` |

## Roadmap

| Milestone | Contents | Status |
| :--- | :--- | :--- |
| **M0** | Repo, licensing, toolchain, gates, ADR-0001 | **done** |
| **M1** | Deterministic ledger core, property and stateful tests | **done** |
| M2 | Trace schema v1, SQLite idempotency, fail-closed redaction | next |
| M3 | Invariant registry, corpus, CLI, first scorecard | |
| M4 | Agent runner, adapters, recorded cassettes | |
| M5 | Full corpus, SARIF/JUnit, security workflows, mutation gate | |
| M6 | Release, drift table, conformance levels | |

## Development

```bash
uv sync --all-groups          # install
make hooks                    # enable the local hooks, including gitleaks
make check                    # local equivalents of the CI gates
make fmt                      # format and autofix
```

Individual gates: `make lint`, `make types`, `make imports`, `make determinism`,
`make licenses`, `make test`, `make cov`, `make audit`, `make secrets`.

`make check` runs the local equivalents of the CI gates. Two differences are deliberate:
the pre-commit hooks are a fast subset that catches problems before a commit exists, and
CI additionally scans the **full Git history** for secrets, not just the working tree.

## Licensing

Source-available, split deliberately:

- `corpus/` and `schema/` are **Apache-2.0**. Adopt, redistribute and cite them freely,
  including in production. Both hold only their license file today; the schema lands in
  M2 and the corpus in M3.
- The runtime under `src/ledgergate/` is **BUSL-1.1**. Read it, modify it, run it in
  development, CI and evaluation. Production use requires a commercial license. Converts
  to Apache-2.0 on 2030-08-31.

See [LICENSING.md](LICENSING.md) and [COMMERCIAL.md](COMMERCIAL.md).
