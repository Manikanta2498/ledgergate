# ADR 0002: A runtime surface, a durable journal, and an authority layer

- Status: Accepted
- Date: 2026-09-03
- Normative detail: [spec/journal.md](../spec/journal.md),
  [spec/trace-v2.md](../spec/trace-v2.md),
  [spec/identifiers-and-redaction.md](../spec/identifiers-and-redaction.md). This ADR
  records the decisions and their trade-offs; the specs are what implementations and
  tests are held to.

## Context

After M0-M2a the project has a deterministic ledger core, a published trace schema, a
recorder and a replayer. The remaining roadmap was entirely *offline*: everything from M3
onward consumed a finished trace and produced a verdict. Nothing let a live agent use the
ledger with the guarantees enforced at the moment of the call.

That left three gaps between the plan and the README's first sentence, "a
correctness-enforcing ledger runtime": no runtime; no notion of *authority* (the ledger
enforces accounting correctness, not whether an agent was allowed to do what it did); no
persistence. Two further weaknesses: per-framework adapters are a treadmill now that the
ecosystem has converged on OpenTelemetry GenAI conventions, and nothing demonstrated the
checks against an agent *trying* to misbehave.

## What this runtime is, and is not

LedgerGate is a **ledger of record and an authorization gate**. It decides whether a
command is admissible, records the decision and its effects durably, and maintains the
books. It does **not** move money on external rails. Calling a payment provider has its
own failure modes (dual writes, provider idempotency, partial settlement, reconciliation),
and claiming SQLite idempotency covers them would be exactly the false guarantee this
project exists to catch. External execution, if built, is M8, with an explicit outbox and
reconciliation. Until then the tools operate on LedgerGate's ledger and nothing else.

## Decisions

### 1. The ledger persists as a strictly append-only journal (M2b)

The journal is the single authoritative artefact; the in-memory ledger is a projection
rebuilt from it; traces are a pure function of it. Every fact is a row, every row has one
position in a single global sequence, and **no row is ever updated**. An idempotency key
is an *operation* (immutable identity) with an appended history of *outcomes*; each caller
attempt is an *invocation*. A retry is therefore visible in the trace as a retry and
invisible to the books as an effect, which is the property the README leads with.

The invariants an implementation is held to are listed in
[spec/journal.md, *Invariants*](../spec/journal.md#invariants); the protocol that
maintains them follows. Two are worth naming here because they changed the design. The
projection cursor is the journal sequence of the latest *outcome* folded in, not the
entry-chain head (lifecycle commands change state without touching the chain) and not the
global maximum (most rows are audit, not state). And every invocation records the exact
outcome that answered it, because "the operation's current outcome" is a different fact
by the time the journal is read.

*Trade-offs:* audited reads serialize with writes; a balance query is cheap, and the
alternative (a deferred read that upgrades to write) can fail after its snapshot is taken.
And a rejected command spends its key in the journal, where the in-memory core leaves it
unspent: the ledger of record treats "what happened to this request" as the answer, even
when what happened is a refusal. Retrying after a refusal is a new request with a new key.

*Placement:* the journal is a new package, `ledgergate.journal`, a sibling of `trace`
above the core: `cli -> runner -> {invariants, report} -> {trace, journal} -> ledger`. It
imports `sqlite3`; the core still may not. The M3 derivation `trace(journal) -> Trace`
depends on both siblings and lives in `ledgergate.invariants`' layer as
`ledgergate.derive`, so the M2b import-linter edit is the final shape. This supersedes the
layer line in ADR-0001, which predates `trace`.

### 2. Authority is a pure layer with explicit inputs (M3)

A policy is a deterministic, versioned function of an explicit, serializable
`PolicyContext`: principal, subject, command digest, evaluation time from the injected
clock, historical aggregates read inside the admitting transaction (so two concurrent
refunds cannot both see "under the cap"), a validated approval if one is presented, and
the policy set version. The full context is persisted with every decision, owned by the
invocation that evaluated it, and carried verbatim in the v2 `policy_decision` event, so an
offline consumer holding the same policy set re-runs the same code on the same inputs; one
without it verifies the recorded evidence and says so.

Approvals are signed artefacts bound to one pending operation and validated before they
enter the context; single use is a database constraint, not a flag. A validated approval
is consumed whatever policy then decides, because every decision on a valid presentation
leaves its operation terminal, so there is nothing left for the artefact to serve. Details
in [spec/journal.md, *Approval artefacts*](../spec/journal.md#approval-artefacts).

### 3. The runtime surface is a local MCP server (M4)

`ledgergate serve` exposes the ledger as Model Context Protocol tools over **stdio only**.
Write tools require an idempotency key and run the journal's write protocol; read tools
run the audited-read protocol and record the journal position they observed. What M4
guarantees: within one local process, for a single local principal, the tools cannot
double-apply, post an unbalanced entry, take an illegal lifecycle step, or exceed the
configured policies. What M4 does not do: listen on a network. Authentication,
multi-tenancy and real approver identity arrive together in M8, because a mandate without
an authenticated principal is not a mandate; the server refuses a network transport until
then.

### 4. Trace schema v2 is built around intents and dispositions (M3)

Schema v1 is frozen. v2's unit is an *intent* with a *disposition* (`new`, `replay`,
`conflict`, `approval`, `read`, `invalid`; plus `legacy` for lifted v1 content, which has
its own grammar because v1 tool events and ledger pairs are not one-to-one). A `policy_decision` appears only when
policy actually ran; a replay never re-evaluates policy and the trace says so. A denied
intent ends at its decision and never reaches the ledger. The runtime derives v2 from the
journal and never derives v1; v1 documents are lifted into the v2 model with disposition
`legacy` and no policy evidence, because inventing an `allow` would be a synthesized
decision.

### 5. Redaction and tokenization happen at admission (M2c)

Free text is fail-closed redacted; caller-supplied identifiers are tokenized with a keyed
HMAC in a fixed format that satisfies the ledger's identifier rule; operator-defined
identifiers are configuration. All of it happens before the ledger hashes anything, so a
redacted trace replays exactly. The earlier claim that identifiers are "not personal data"
is withdrawn: the code accepts an email address as an account id.

### 6. OpenTelemetry GenAI is the primary *observational* adapter (M5)

The journal is authoritative. An OTel adapter maps `gen_ai.*` spans to trace events,
validates completeness against the v2 contract, and yields either a conforming trace or
a report of exactly what was missing. It requires unsampled capture. Per-framework
adapters, where they exist, are conveniences over it.

### 7. The corpus includes a red team (M6)

Alongside scenarios that exercise correct behaviour, traces of agents behaving badly:
prompt-injected, retrying without keys, jumping lifecycle states, exceeding limits. The
suite's claim is that these are stopped; the red-team corpus is the evidence.

## Explicitly rejected

- **LLM-as-a-judge in any check.** Financial correctness is an assertion, not an opinion.
- **An agent framework of our own.** The value is in being usable from all of them.
- **Retrieval, memory, vector stores.** Nothing here needs them.
- **Network MCP before authentication.** An unauthenticated listener applying "policy" is
  theatre.
- **Deriving v1 from the journal.** The journal has more structure than v1 can carry; a
  lossy projection is a second thing to keep consistent.

## Roadmap

| Milestone | Contents |
| :--- | :--- |
| M2b | The journal per [spec/journal.md](../spec/journal.md): tables, write and audited-read protocols, projection with outcome cursor, approval machinery present and tested empty. Ships with the identity admitter and the null policy set so the protocol shape is complete; derives no trace |
| M2c | The tokenizing, redacting admitter per [spec/identifiers-and-redaction.md](../spec/identifiers-and-redaction.md), replacing M2b's identity admitter behind the same interface |
| M3 | Trace schema v2 and journal-to-v2 derivation per [spec/trace-v2.md](../spec/trace-v2.md); `PolicyContext` and real policy sets replacing the null policy; invariant registry; scorecard; `ledgergate verify` |
| M4 | `ledgergate serve`: stdio MCP, single local principal, journal protocol on every call |
| M5 | OpenTelemetry GenAI observational adapter with completeness validation; thin wrappers; cassettes |
| M6 | Scenario corpus and red-team corpus; SARIF/JUnit; drift table across model versions |
| M7 | Mutation gate, CodeQL, OpenSSF Scorecard, PyPI release, conformance levels |
| M8 | Authenticated network transport and principals; real approvers; external execution via outbox and reconciliation |

## Consequences

- One durable truth, strictly append-only. Traces are a function of it; the v2 replayer
  checks a journal against its own projection.
- A journal is bound to one ledger definition. Reconfiguring means a new journal; migration
  is an explicit, replayed operation.
- Policies are versioned from day one: the version is in every decision row and every v2
  policy event.
- M4 ships as a local tool with real guarantees inside a stated boundary, rather than a
  network service with claimed guarantees outside it.
- The runtime ships before the full corpus. The runtime is what the project is; the corpus
  is how it is proven.
- Infrastructure failures (SQLite unavailable, a bug in the core) leave no journal row.
  This is the one class of unrecorded call, and it is stated rather than hidden.

## History

Thirteen revisions on 2026-09-03 moved this document from guarantees without mechanisms,
to mechanisms without invariants, to a mutable row that defeated its own cursor, to a
protocol that never inserted the row everything referenced and consumed approvals before
validating them. Each round pushed a check earlier and removed a place where two things
could disagree. The normative detail that accumulated was moved to `docs/spec/` so this
record can stay a record.
