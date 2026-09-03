# ADR 0002: A runtime surface, a durable command log, and an authority layer

- Status: Accepted
- Date: 2026-09-03
- Supersedes: the M2-M6 roadmap as written in ADR-0001's era

## Context

After M0-M2a the project has a deterministic ledger core, a published trace schema, a
recorder and a replayer. The remaining roadmap (invariant registry, corpus, adapters,
scorecards) was entirely *offline*: everything from M3 onward consumed a finished trace
and produced a verdict. Nothing let a live agent use the ledger as its money-moving tool
with the guarantees enforced at the moment of the call.

That left three gaps between the plan and the README's first sentence, "a
correctness-enforcing ledger runtime":

1. **No runtime.** The enforcing half of the pitch had no milestone.
2. **No notion of authority.** The ledger enforces accounting correctness (balance,
   lifecycle, idempotency). It has no concept of whether an agent was *allowed* to do
   what it did: refund limits, approval thresholds, velocity caps. Those are the
   guardrails a fintech risk team actually writes.
3. **No persistence.** The ledger is in-memory. A runtime must survive a restart.

Two further weaknesses: per-framework adapters ("anthropic | openai | langgraph") are a
treadmill when the ecosystem has converged on OpenTelemetry GenAI semantic conventions;
and nothing in the plan demonstrated the checks against an agent *trying* to misbehave.

## Decision

**The ledger persists as an append-only command log (M2b).** SQLite in WAL mode, one row
per command, `UNIQUE` on the idempotency key so atomicity comes from the database rather
than from check-then-write. The in-memory ledger is a projection: on start, replay the
log through the pure core. This is event sourcing, and it falls out of what exists: the
trace already *is* a command log with recorded effects, and `replay()` already rebuilds a
ledger from one. Idempotency across restarts is a consequence, not a separate feature.

**Authority is a pure layer above the ledger (M3).** A policy is a deterministic,
versioned rule evaluated over the same inputs the ledger sees: "refunds above 500.00 USD
require approval", "no more than three refunds per customer per day", "cross-currency
movements are never autonomous". Policies are checked offline over traces (as invariants
are) and online at the runtime's call boundary (M4), by the same code. A policy violation
is a first-class outcome in the trace, so it replays and is checked like any other.

**The runtime surface is an MCP server (M4).** `ledgergate serve` exposes the ledger as
Model Context Protocol tools: `open_transaction`, `authorize`, `settle`, `refund`,
`balance`, and so on. Every tool requires an idempotency key; every call is checked
against policy before it reaches the ledger; every call and its outcome is recorded as a
trace event. Any MCP client, whatever model or framework drives it, gets a money-moving
tool that cannot double-refund, cannot post an unbalanced entry, and cannot exceed its
mandate. MCP is chosen because it is the one tool protocol the major clients share; it
turns "framework-agnostic" from a schema property into a deployment property.

**OpenTelemetry GenAI is the primary adapter (M5).** Frameworks already emit `gen_ai.*`
spans. One OTel-to-trace adapter covers them; per-framework adapters, where they exist at
all, are thin conveniences over it.

**The corpus includes a red team (M6).** Alongside scenarios that exercise correct
behaviour, a set of traces from agents behaving badly: prompt-injected, retrying without
keys, jumping lifecycle states, exceeding limits. The suite's claim is that these are
stopped; the red-team corpus is the evidence.

## Explicitly rejected

- **LLM-as-a-judge in any check.** The thesis is that financial correctness is an
  assertion, not an opinion. A probabilistic judge anywhere in the verdict path would
  undercut it.
- **An agent framework of our own.** The value is in being usable from all of them.
- **Retrieval, memory, vector stores.** Nothing here needs them.

## Roadmap

| Milestone | Contents |
| :--- | :--- |
| M2b | Durable command log; ledger as projection; idempotency across restarts |
| M2c | Fail-closed redaction: allowlist, deterministic tokens, redacted traces still replay |
| M3 | Invariant registry over traces; policy layer; scorecard; `ledgergate verify` |
| M4 | `ledgergate serve`: MCP runtime, policy at the call boundary, every call traced |
| M5 | OpenTelemetry GenAI adapter; thin framework wrappers; recorded cassettes |
| M6 | Scenario corpus and red-team corpus; SARIF/JUnit; drift table across models |
| M7 | Mutation gate, CodeQL, OpenSSF Scorecard, PyPI release, conformance levels |

## Consequences

- The command log becomes a second durable artefact alongside the trace, and they must
  agree; replay is the check.
- Policies need a versioning story from day one: a trace must record which policy
  version judged it, or drift comparisons are meaningless.
- The MCP server is the first component with a network boundary. Authentication,
  multi-tenancy and rate limiting are deliberately out of scope for M4; the server is
  single-tenant and local-first. That boundary is stated, not assumed.
- Moving the runtime ahead of the corpus means M4 ships with accounting invariants and
  whatever policies M3 defines, not with a full scenario library. That is the right
  trade: the runtime is what the project is, and the corpus is how it is proven.
