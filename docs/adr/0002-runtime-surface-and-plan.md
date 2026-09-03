# ADR 0002: A runtime surface, a durable command log, and an authority layer

- Status: Accepted
- Date: 2026-09-03
- Amended: 2026-09-03 (twice), after review found first that the guarantees lacked
  mechanisms, then that the mechanisms lacked invariants. Amendments are marked.

## Context

After M0-M2a the project has a deterministic ledger core, a published trace schema, a
recorder and a replayer. The remaining roadmap was entirely *offline*: everything from M3
onward consumed a finished trace and produced a verdict. Nothing let a live agent use the
ledger with the guarantees enforced at the moment of the call.

That left three gaps between the plan and the README's first sentence, "a
correctness-enforcing ledger runtime":

1. **No runtime.** The enforcing half of the pitch had no milestone.
2. **No notion of authority.** The ledger enforces accounting correctness (balance,
   lifecycle, idempotency). It has no concept of whether an agent was *allowed* to do
   what it did: refund limits, approval thresholds, velocity caps.
3. **No persistence.** The ledger is in-memory. A runtime must survive a restart.

Two further weaknesses: per-framework adapters are a treadmill when the ecosystem has
converged on OpenTelemetry GenAI semantic conventions; and nothing in the plan
demonstrated the checks against an agent *trying* to misbehave.

## What this runtime is, and is not

*(Amended: the first version called the M4 tool "money-moving", which overstated it.)*

LedgerGate is a **ledger of record and an authorization gate**. It decides whether a
command is admissible, records the decision and its effects durably, and maintains the
books. It does **not** move money on external rails. Calling a payment provider is a
separate concern with its own failure modes (dual writes, provider idempotency, partial
settlement, reconciliation), and pretending SQLite idempotency covers it would be the
exact kind of false guarantee this project exists to catch.

External execution, if it is ever built, is a later milestone (M8 in the roadmap below)
with an explicit outbox: the ledger records an *authorized intent*, an executor consumes
the outbox with the provider's own idempotency key, and a reconciliation step closes the
loop or records the discrepancy. Until then, the MCP tools operate on LedgerGate's ledger
and nothing else, and the README says so.

## Decision 1: the ledger persists as an append-only log of operations and invocations (M2b)

*(Amended twice. The first version said "one row per command plus UNIQUE(key)". The
second gave that row a protocol. Review of the second found that one unique row cannot
record a retry, that the entry-chain head is not a completeness cursor, and that "trace as
outbox" needs a derivation rule. This version separates what was conflated.)*

**Authority.** The log is the single authoritative artefact. The in-memory ledger is a
projection rebuilt from it. Traces are a *pure function* of the log, computed on demand
or materialized, never written by a second path. That is why the two cannot disagree:
there is no second writer, so there is nothing to reconcile.

**Two things that the first drafts conflated.** An *operation* is the durable fact about
an idempotency key: what was asked, once, and what the answer is. An *invocation* is one
attempt by a caller, of which a key may see many. The first invocation of a key creates
its operation; every later one with a matching fingerprint is a replay of it; one with a
different fingerprint is a conflict. `replayed` is a property of an invocation, never of an
operation. The ledger rebuilds from operations; the trace derives from invocations.

**Tables.** All append-only except the one transition noted under `operations.outcome`.

| Table | Holds |
| :--- | :--- |
| `definition` | Written once: chart, currency registry, codec version, policy set version, identifier token domain and key version. A log is bound to one definition; changing it means a new log. |
| `operations` | `sequence` (PK), `key` (`UNIQUE`), `fingerprint`, canonical `command`, `outcome` (`applied`, `rejected`, `denied`, `awaiting_approval`), error type and message, `entry_id`/`posted_at` when appended, `head_before`, `head_after`, `ledger_sequence`, `decision_id` |
| `invocations` | `sequence` (PK), `operation_sequence`, `requested_at`, `response` (`applied`, `rejected`, `denied`, `awaiting_approval`, `replayed`, `conflict`), `call_id` linkage, principal |
| `decisions` | `id`, the canonical serialized `PolicyContext`, policy set version, decision, matched rule, reason, approval reference. One per operation that reached policy. |
| `events` | Message, `tool_call` and `tool_result` events the runtime receives at its boundary, with their `invocation_sequence` where one applies |

`operations.outcome` has exactly one legal transition: `awaiting_approval` to a terminal
outcome, performed by a later invocation of the same key and fingerprint that carries a
valid approval. The approval is part of the `PolicyContext`, not of the fingerprint, so
the retry matches. The invocation row records the transition, so history is preserved
even though the operation row is updated. Every other outcome is terminal; a retry of a
rejected or denied key replays the rejection or denial.

**Transaction protocol.** One invocation, one SQLite transaction, `BEGIN IMMEDIATE`.
SQLite serializes write transactions; there is exactly one active at a time, whatever
the process count.

1. Take the write lock.
2. **Confirm the projection is current.** The projection carries
   `last_applied_operation_sequence`. If it is not the log's max, rebuild from
   `definition` and all operations. The entry-chain head is *not* the cursor: lifecycle
   commands change transaction state without touching the chain, so a projection can have
   the right head and still be stale. The head is kept as an integrity check on the
   rebuilt projection, not as a freshness test.
3. Look up `key`. Present with matching fingerprint: insert an invocation row
   (`replayed`, or the approval transition if applicable), commit, return the operation's
   outcome. Present with a different fingerprint: insert an invocation row (`conflict`),
   commit, return the conflict. Absent: continue.
4. Evaluate policy over the `PolicyContext`, reading any velocity aggregates from
   `operations` and `decisions` *inside this transaction*. Write the `decisions` row. If
   the decision is not `allow`, write the operation (`denied` or `awaiting_approval`) and
   invocation rows, commit, return.
5. Run the command through the pure core. Deterministic and cheap.
6. Write the operation row (`applied` or `rejected`, with effects and heads) and the
   invocation row. Commit.
7. Only now return the result to the caller.

A crash before commit leaves nothing: no key claimed, no result, no trace. A retry runs
afresh. A crash after commit but before step 7 leaves a complete operation; the retry
hits step 3 and gets the stored outcome, recorded as one more invocation. At no point
does a key exist without its complete result, and at no point does the ledger apply
twice.

**Trace derivation.** `trace(log) -> Trace` is deterministic. Event identity is
`(invocation_sequence, kind)`; ordering is the log's; there is no consumer offset because
there is no queue. Each invocation yields a `command_intent`, a `policy_decision`, and
(only when the policy allowed and the invocation is the first for its key) a
`ledger_command`/`ledger_result` pair, all in schema v2 (Decision 4). Messages and tool
events come from `events`. The M2a replayer, run on the derived trace, is the consistency
check between a log and its projection.

**Concurrency.** Any number of readers under WAL; write transactions serialized by
SQLite. Multiple writer processes are correct under step 2 and not optimized.

## Decision 2: authority is a pure layer with explicit inputs (M3)

*(Amended: the first version said policy sees "the same inputs the ledger sees". The
example policies need inputs the ledger does not have.)*

A policy is a deterministic, versioned function of a **`PolicyContext`**, not of the
command alone. The context is explicit, serializable, and recorded with the decision:

- **Principal**: who is asking. In M4 this is the single local principal; identity
  becomes meaningful only when a transport with authentication exists (see Decision 3).
- **Subject**: the customer or account the command concerns, so per-customer rules have
  something to key on.
- **Command digest**: the fingerprint of what is being decided.
- **Evaluation time**: from the injected clock, so decisions replay.
- **Historical state**: the aggregates a velocity rule needs (refunds by this subject in
  this window), read *inside the same transaction* as admission, so two concurrent
  refunds cannot both observe "under the cap" and both pass.
- **Approval**, when present: an artefact naming the approver, the scope, an expiry, and
  whether it has been consumed. Consumption is recorded in the same transaction.
- **Policy set version**: which rules judged this.

A decision is `allow`, `deny`, or `approval_required`, with the matched rule and reason.
Policies stay pure: given the same context they return the same decision, so offline
evaluation over a trace and online evaluation at the boundary run the same code on the
same inputs.

*(Amended: where the context is stored.)* The full canonical `PolicyContext`, not a
summary of it, is written to the `decisions` table in the same transaction as the
operation it judged, including the historical aggregate values the velocity rules read
and the approval artefact if one was consumed. Replaying a decision needs no access to
live state: everything it evaluated is in the row.

## Decision 3: the runtime surface is a local MCP server (M4)

*(Amended: the first version promised "any MCP client" a tool that "cannot exceed its
mandate" while leaving authentication out of scope. Without an authenticated principal
there is no mandate to enforce, so the claim did not hold. The scope is now explicit.)*

`ledgergate serve` exposes the ledger as Model Context Protocol tools over **stdio only**:
`open_transaction`, `authorize`, `settle`, `refund`, `balance`, `trial_balance`. Every tool
requires an idempotency key; every call runs the M2b protocol, so it is checked, recorded
and durable before it returns; every call and outcome becomes a trace event via the log.

**What M4 guarantees:** within one local process, the tools cannot double-apply, cannot
post an unbalanced entry, cannot take an illegal lifecycle step, and cannot exceed the
policies configured for the single local principal.

**What M4 does not do:** listen on a network. There is no HTTP or SSE transport, no
authentication, no multi-tenancy, and therefore no per-user mandate or approver identity
beyond the local principal. Those arrive together in a later milestone (M8) because
authority claims are meaningless without an authenticated principal to attach them to. A
network transport before that would be an unsupported configuration, and the server
refuses to start with one.

MCP is chosen because it is the one tool protocol the major clients share. Stdio is its
default transport and is exactly the boundary M4 can defend.

## Decision 4: trace schema v2 is built around intents (M3)

*(Amended twice. The second version said v2 "adds a policy_decision event" without
saying what a denied request's command/result pair looks like. Under v1's rule, a
`ledger_command` requires a `ledger_result`, but a denied command never reaches the
ledger. v2 therefore cannot be v1 plus one event.)*

Schema v1 is frozen as published. M3 publishes **schema v2**, in which the unit is an
**intent**: a proposed command, identified before anything decides on it.

```
tool_call
  command_intent      intent_id, command, context digest        exactly one per invocation
  policy_decision     intent_id, decision, rule, policy version exactly one per intent
  [ledger_command     intent_id                                  iff decision == allow
   ledger_result]     command_id                                 iff ledger_command
tool_result
```

Cardinality and order are rules of the schema description, enforced by the models as v1's
are. A `deny` or `approval_required` intent ends at its decision and is replayed by
policy alone; an `allow` intent continues into the v1-style pair and is replayed by both
policy and ledger. A later invocation that resolves an `awaiting_approval` operation is a
new intent whose decision carries the approval reference.

A v1 document maps into v2 by giving each `ledger_command` an intent whose decision is
`allow` under policy version `none`; that is how the v1 replayer's guarantees carry over.
The runtime reads v1 and v2 and writes v2.

## Decision 5: redaction at admission; caller identifiers are tokenized (M2c)

*(Amended twice. The second version declared identifiers "structural, not personal data".
Nothing enforces that: `Account("customer@example.com", ...)` is accepted today, and a
caller can put a phone number in an idempotency key. The claim is withdrawn and replaced
with a mechanism.)*

Three classes of field, three treatments, all applied **before the ledger hashes
anything**, so every digest is computed over the stored form and a trace replays exactly:

1. **Free text** (`description`, message `content`, tool `arguments` and `result`, tag
   values): fail-closed redaction. A field not on the allowlist is redacted. Replacement
   tokens are deterministic (keyed HMAC), so equal inputs redact equally across runs.
2. **Caller-supplied identifiers** (`transaction_id`, idempotency keys, `call_id`,
   `trace_id`): deterministically tokenized with the same keyed HMAC at admission. The
   caller retries with the raw key; it tokenizes to the same value; the lookup works.
   Replay uses the stored tokens and needs no key. The token domain and key version are
   in `definition`; rotating the key means a new log, and cross-log correlation is an
   explicit operation, not an accident.
3. **Operator-defined identifiers** (`account_id`, tool names): configuration, written by
   the operator into the ledger definition, not by callers or agents at runtime. They are
   stored as given. The definition loader warns on values that look like emails, phone
   numbers or card numbers; the operator owns what they name their accounts.

Amounts, currencies, sides and account references remain in the clear; they are the
books, and a ledger whose amounts are redacted is not a ledger. M2c covers schema v1
fields; v2's intent and policy fields are designed under the same three classes in M3.

## Decision 6: OpenTelemetry GenAI is the primary *observational* adapter (M5)

*(Amended: the first version implied one OTel adapter yields authoritative traces.)*

Runtime-native recording (the log) is authoritative. An OTel adapter produces
*observed* traces: it maps `gen_ai.*` spans to trace events, validates completeness
against the v2 contract (one call, one result, ordered), and either yields a conforming
trace or a report of exactly what was missing. M5 fixes the required semantic-convention
version and attributes, and requires unsampled capture for a trace to be admitted at all.
Per-framework adapters, where they exist, are conveniences over the OTel one.

## Decision 7: the corpus includes a red team (M6)

Alongside scenarios that exercise correct behaviour, a set of traces from agents behaving
badly: prompt-injected, retrying without keys, jumping lifecycle states, exceeding limits.
The suite's claim is that these are stopped; the red-team corpus is the evidence.

## Explicitly rejected

- **LLM-as-a-judge in any check.** The thesis is that financial correctness is an
  assertion, not an opinion.
- **An agent framework of our own.** The value is in being usable from all of them.
- **Retrieval, memory, vector stores.** Nothing here needs them.
- **Network MCP before authentication.** An unauthenticated network listener that
  applies "policy" is theatre.

## Roadmap

| Milestone | Contents |
| :--- | :--- |
| M2b | Operations, invocations, decisions and events tables; definition table; the transaction protocol; projection cursor; deterministic trace derivation |
| M2c | Redaction and identifier tokenization at admission; token domain in the definition; redacted traces replay |
| M3 | Trace schema v2 around intents; `PolicyContext` persisted per decision; policy layer with in-transaction velocity state; approval transition; invariant registry; scorecard; `ledgergate verify` |
| M4 | `ledgergate serve`: stdio MCP, single local principal, log protocol on every call |
| M5 | OpenTelemetry GenAI observational adapter with completeness validation; thin wrappers; cassettes |
| M6 | Scenario corpus and red-team corpus; SARIF/JUnit; drift table across model versions |
| M7 | Mutation gate, CodeQL, OpenSSF Scorecard, PyPI release, conformance levels |
| M8 | Authenticated network transport and principals; approval artefacts with real approvers; external execution via outbox and reconciliation |

## Consequences

- The log is the one durable truth. Traces are a deterministic function of it. The M2a
  replayer, run on the derived trace, checks a log against its own projection.
- Operations and invocations are distinct, so a retry is visible in the trace as a retry
  and invisible to the ledger as an effect, which is exactly the property the README
  leads with.
- A log is bound to one ledger definition. Reconfiguring means a new log, and migration
  between logs is an explicit, replayed operation, not an edit.
- Policies need a versioning story from day one, and they have one: the version is in
  every decision row and every v2 policy event.
- M4 ships as a local tool with real guarantees inside its stated boundary, rather than
  a network service with claimed guarantees outside it. The README describes it in those
  terms.
- Moving the runtime ahead of the corpus means M4 ships with accounting invariants and
  M3's policies, not a full scenario library. That is the right trade: the runtime is
  what the project is, and the corpus is how it is proven.
