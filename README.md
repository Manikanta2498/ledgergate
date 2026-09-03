# LedgerGate

**A correctness-enforcing ledger runtime for autonomous agents that move money, plus the
invariant conformance suite that proves an agent respects financial state machines before
deployment.**

> **Status: pre-alpha, milestone M2 complete.** The deterministic ledger core (M1),
> the trace schema that makes it framework-agnostic (M2a), the durable journal (M2b) and
> the tokenizing, redacting admitter (M2c) are implemented and tested. An agent run can be recorded, validated against the published
> schema, replayed against the core, and journaled so a retried key after a restart gets
> the answer it got the first time. The invariant suite, policy layer, corpus, adapters and
> the runtime CLI are not built yet; `ledgergate journal dump` inspects a journal. See
> [Roadmap](#roadmap).

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
                  offline: prove it before deploy
agent run ──▶ trace (schema v1 | v2) ──▶ invariants + policy ──▶ result.json ──▶ md | junit | sarif
                    ▲
       adapters: OpenTelemetry GenAI | openai | anthropic | langgraph

                  online: enforce it at the call boundary
MCP client ──▶ ledgergate serve (stdio) ──▶ policy ──▶ command log ──▶ ledger + trace
```

The interop contract is [`schema/trace/v1.json`](schema/trace/v1.json), a JSON Schema
2020-12 document, not our harness. Any agent that emits the schema can be checked,
whatever framework it uses.

The same ledger, policy and trace serve both paths. Offline, a recorded run is checked
before an agent ships. Online, the MCP server is the agent's ledger of record and
authorization gate: every call is checked at the boundary, written to a durable command
log before it returns, and derived into the same trace format, so runtime traces feed
straight back into the offline checks. LedgerGate keeps the books and decides what is
admissible; it does not itself move money on external rails (see ADR-0002).

**What exists today:** the ledger core; the trace schema, recorder and replayer; and the
durable journal, so a process can be restarted and answer a retried key exactly as it did
the first time. Invariants and policy land in M3, the MCP runtime in M4. The gates that keep all of it honest run in CI on every pull request and every push
to `main`.

## The trace schema

A trace is the normalized record of one agent run: what the agent said and called, which
ledger commands the system derived from that, and what the ledger answered. It is the
boundary between "your agent" and "our checks".

```python
import json

from ledgergate.ledger import *
from ledgergate.trace import *

chart = ChartOfAccounts(
    [Account("cash", AccountType.ASSET, USD), Account("revenue", AccountType.REVENUE, USD)]
)
rec = Recorder(
    "run-1",
    AgentDoc(name="refund-bot", model="gpt-x"),
    chart,
    SteppingClock(EPOCH),
    SequentialIds(),
)

sale = EntryDraft.of(debit("cash", Money(1999, USD)), credit("revenue", Money(1999, USD)))
rec.tool_call("c1", "refund_order", {"order": "42"}, idempotency_key="refund-42")
rec.execute(OpenTransaction("open-42", "order-42", Money(1999, USD)), call_id="c1")
rec.execute(Advance("auth-42", "order-42", TransactionEvent.AUTHORIZE))
rec.execute(Advance("settle-42", "order-42", TransactionEvent.SETTLE, sale))
rec.execute(Refund("refund-42", "order-42", Money(1999, USD), sale.reversed()))
rec.execute(Refund("refund-42", "order-42", Money(1999, USD), sale.reversed()))  # the retry
rec.tool_result("c1", ok=True, result={"status": "refunded"})  # exactly one result per call

text = dump_trace(rec.trace())  # canonical JSON: sorted keys, byte-stable
validate_document(json.loads(text))  # against schema/trace/v1.json
report = replay_trace(load_trace(text))  # re-run every command through the core
assert report.consistent  # recorded heads, sequences and replays all recompute
```

Edit one recorded `head`, or claim the retry was not a replay, and `report.divergences`
names the command and the field. That replay is the mechanism the M3 invariants are
built on.

A trace records what was *attempted*, not only what succeeded: a zero-amount refund is
representable, the ledger's rejection of it is recorded as the result, and replay confirms
the rejection. Every result has a fixed shape for its outcome (success carries `replayed`,
`head`, `sequence` and the consumed effects; failure carries `error`, `head`, `sequence`),
so a sparse result cannot pass as consistent. Currencies travel with their minor-unit
exponents, so a trace using a currency this runtime does not bundle still replays exactly.

The schema and the runtime models are held to each other by contract tests on both valid
and invalid documents. The rules JSON Schema cannot express are listed in the schema's own
description and enforced by the models: `seq` strictly increases; every ledger command
and every tool call has exactly one result after it, with none orphaned; ids are unique;
every currency code resolves; tool payloads are bounded in depth and size. One further
asymmetry is pinned: the runtime refuses a whole float like `5.0` where the schema's
`integer` must admit it, because the JSON data model has one number type.

## The journal

The runtime's durable truth is an append-only SQLite journal, specified in
[`docs/spec/journal.md`](docs/spec/journal.md) and implemented in `ledgergate.journal`.
The in-memory ledger is a projection rebuilt from it.

```python
from ledgergate.journal import Journal
from ledgergate.ledger import (
    USD,
    Account,
    AccountType,
    ChartOfAccounts,
    SequentialIds,
    SteppingClock,
    EPOCH,
)

chart = ChartOfAccounts(
    [Account("cash", AccountType.ASSET, USD), Account("revenue", AccountType.REVENUE, USD)]
)
journal = Journal.create(path, chart, clock=SteppingClock(EPOCH), ids=SequentialIds())

sale = {
    "postings": [
        {"account": "cash", "side": "debit", "money": {"amount": 1999, "currency": "USD"}},
        {"account": "revenue", "side": "credit", "money": {"amount": 1999, "currency": "USD"}},
    ]
}
first = journal.handle(
    {"tool": "post", "call_id": "c1", "key": "order-42", "arguments": {"draft": sale}}
)
retry = journal.handle(
    {"tool": "post", "call_id": "c2", "key": "order-42", "arguments": {"draft": sale}}
)
assert first.response == "applied" and retry.response == "replayed"
assert retry.result["entry_id"] == first.result["entry_id"]

changed = journal.handle(
    {
        "tool": "post",
        "call_id": "c3",
        "key": "order-42",
        "arguments": {"draft": {**sale, "description": "different"}},
    }
)
assert changed.response == "conflict"

journal.close()
reopened = Journal.open(path, clock=SteppingClock(EPOCH), ids=SequentialIds(start=100))
assert reopened.ledger.head == first.result["head"]  # rebuilt from outcomes, not trusted
again = reopened.handle(
    {"tool": "post", "call_id": "c4", "key": "order-42", "arguments": {"draft": sale}}
)
assert again.response == "replayed"  # idempotency survives the restart
reopened.close()
```

One invocation is one `BEGIN IMMEDIATE` transaction; the response is rendered only after
commit. Every attempt is a row, including malformed input, with one stated exception: the
unrecorded-failure class in the spec (input that is not I-JSON, a fault of the process's own
clock or id generator, the database being unavailable, an integrity failure), where the
transaction rolls back and the caller gets an error instead of a row. No row is ever
updated or deleted (the database refuses, not the code). A rejected command spends its key: the
rejection *is* the recorded result, and a retry replays it. Every journal digest
(`request_digest`, `input_digest`, `result_digest`) is SHA-256 over RFC 8785 canonical JSON;
result amounts are decimal strings and argument amounts are the I-JSON integers the caller
sent, so a JavaScript client and this runtime agree byte for byte. The operation
fingerprint and the hash chain are the core's own length-prefixed encoding. The shipped policy set is the
null set (`none`), which allows everything and still writes a complete decision row; real
policy arrives in M3 behind the same interface.

**Redaction and tokenization (M2c).** No caller identifier or free text has to reach disk.
With a
`TokenizingAdmitter` (or a `Recorder(redactor=...)` for traces), every caller identifier
(idempotency keys, transaction ids, call ids) becomes a keyed HMAC token *before* the
command is fingerprinted, looked up or written, so a later `settle` with the raw id finds
the transaction the earlier `open_transaction` stored, and a retry with the raw key replays;
every free-text field (descriptions, tags, message content, tool arguments and results,
account names) becomes a deterministic replacement. Amounts, currencies, sides and
account references stay in the clear: they are the books. Every digest was computed over
the stored form, so the fold that rebuilds a journal and the replay of a trace need no key;
opening a journal *for admission* requires the key that created it, and a different key
under the same label is detected and refused. The identity admitter, which changes
nothing, remains available for development.

```python
from ledgergate.codec import Tokenizer
from ledgergate.journal import Journal, TokenizingAdmitter
from ledgergate.ledger import (
    EPOCH,
    USD,
    Account,
    AccountType,
    ChartOfAccounts,
    SequentialIds,
    SteppingClock,
)

chart = ChartOfAccounts(
    [Account("cash", AccountType.ASSET, USD), Account("revenue", AccountType.REVENUE, USD)]
)

tokenizer = Tokenizer(
    key_bytes, domain="acme", key_version="2026-q1"
)  # >= 16 random bytes, kept by the operator
journal = Journal.create(
    path2,
    chart,
    clock=SteppingClock(EPOCH),
    ids=SequentialIds(),
    admitter=TokenizingAdmitter(tokenizer),
)
r = journal.handle(
    {
        "tool": "open_transaction",
        "call_id": "c1",
        "key": "order-42",
        "arguments": {"transaction_id": "txn-alice", "amount": {"amount": 1999, "currency": "USD"}},
    }
)
assert r.result["transaction"]["id"].startswith("tk1_acme_")  # the raw id never reached the journal
journal.close()
```

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
| Idempotency | Every command carries a key; same key + same request replays the original result, same key + different request raises. In the in-memory core a rejected command leaves the key unspent; in the durable journal (M2b) a rejection is itself the recorded result and the key is spent, so retrying after a rejection means a new key. Fingerprints are SHA-256 over an unambiguous length-prefixed encoding, so no delimiter trick can make two different requests serialize the same |
| Lifecycle | `PENDING -> AUTHORIZED -> SETTLED -> PARTIALLY_REFUNDED -> REFUNDED` plus dispute, cancel, fail; illegal moves raise. `SETTLE` and `REFUND` *require* a journal entry that moves exactly the stated amount in the transaction's currency, posted atomically with the transition; other events must not carry one |
| FX | Balanced four-line conversion through clearing accounts, so cross-currency moves cannot leak |
| Effects | `Clock`, `IdGenerator`, `FxRateSource` Protocols with deterministic reference implementations |
| Replay | `replay(chart, commands, clock=..., ids=...)` reproduces an equal ledger: same entries, same digests, same head hash |
| Trace | `ledgergate.trace`: typed models mirroring the schema, `Recorder` to capture a session, canonical `dump_trace`, `validate_document` against the published schema, `replay_trace` that re-executes and diffs |

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
| **M2a** | Trace schema v1, recorder, replay | **done** |
| **M2b** | Strictly append-only journal with one global sequence: operations (one per key), outcomes (appended, never edited), invocations (one per attempt), decisions, single-use (per journal) approvals, boundary events. One attempt, one transaction, response returned only after commit. Ledger is a projection with an outcome cursor. Ships with a pass-through admitter and a null policy so the protocol is complete end to end; trace derivation follows in M3 | **done** |
| **M2c** | The real admitter: free text fail-closed redacted, caller identifiers tokenized, both before the ledger hashes anything, so redacted traces replay exactly | **done** |
| M3 | Trace schema v2 built around *intents* and *dispositions* (a denied command never reaches the ledger, a retry never re-evaluates policy, an imported v1 trace carries no invented policy evidence or tool events, and the schema says all of it), with journal-to-trace derivation; **policy layer** over an explicit, persisted `PolicyContext` carried in every decision event, with validated, single-use (per journal) approvals; invariant registry; scorecard; `ledgergate verify` | next |
| M4 | **`ledgergate serve`: local MCP runtime** (stdio, single principal). The ledger as tools, idempotency required, policy enforced at the call boundary, every call through the command log | |
| M5 | OpenTelemetry GenAI *observational* adapter with completeness validation; thin framework wrappers; recorded cassettes | |
| M6 | Scenario corpus and **red-team corpus**; SARIF/JUnit; drift table across model versions | |
| M7 | Mutation gate, CodeQL, OpenSSF Scorecard, PyPI release, conformance levels | |
| M8 | Authenticated network transport and principals; real approvers; external execution via outbox and reconciliation | |

The reasoning behind this order, and what was deliberately left out, is in
[ADR-0002](docs/adr/0002-runtime-surface-and-plan.md). The normative protocols the
milestones are built to are in [`docs/spec/`](docs/spec/).

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
  including in production. The trace schema is published; the corpus lands in M6.
- The runtime under `src/ledgergate/` is **BUSL-1.1**. Read it, modify it, run it in
  development, CI and evaluation. Production use requires a commercial license. Converts
  to Apache-2.0 on 2030-08-31.

See [LICENSING.md](LICENSING.md) and [COMMERCIAL.md](COMMERCIAL.md).
