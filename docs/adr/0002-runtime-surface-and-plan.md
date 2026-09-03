# ADR 0002: A runtime surface, a durable command log, and an authority layer

- Status: Accepted
- Date: 2026-09-03
- Amended: 2026-09-03 (three times), after review found first that the guarantees lacked
  mechanisms, then that the mechanisms lacked invariants, then that one mutable row
  undermined the cursor built to protect it. Amendments are marked.

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

## Decision 1: the ledger persists as a strictly append-only journal (M2b)

*(Amended three times. The third review found that the single mutable transition kept in
revision 2, `awaiting_approval` to a terminal outcome, does not advance any sequence, so
the projection cursor introduced in that same revision would report a stale process as
current. Nothing is mutable now. It also found that boundary tool events were outside the
transaction and that replays and conflicts could not satisfy "one policy decision per
intent". All three are fixed by the same move: every fact is a row, every row has one
global position, and every row a response depends on is committed before the response.)*

**Authority.** The journal is the single authoritative artefact. The in-memory ledger is
a projection rebuilt from it. Traces are a *pure function* of the journal, never written
by a second path, so there is nothing to reconcile.

**Vocabulary.** An *operation* is the immutable identity of an idempotency key: what was
asked, once. An *outcome* is a fact about an operation at a point in time; an operation
has one or more, appended, never edited. An *invocation* is one caller attempt; a key may
see many. A *decision* is one policy evaluation, owned by the invocation that triggered
it. `replayed` and `conflict` are properties of invocations. The ledger rebuilds from
outcomes; the trace derives from invocations and events.

**One global order.** Every row in every table below carries a `journal_sequence` drawn
from a single monotonic counter. That is the projection cursor, the trace order, and the
only ordering source. Per-table sequences do not exist.

| Table | Holds |
| :--- | :--- |
| `definition` | Written once: chart, currency registry, codec version, policy set version, identifier token domain and key version. A journal is bound to one definition; changing it means a new journal. |
| `operations` | `key` (`UNIQUE`), `fingerprint`, canonical `command`. Immutable identity only. |
| `outcomes` | `operation` reference, `previous_outcome`, `outcome` (`applied`, `rejected`, `denied`, `awaiting_approval`), error type and message, `entry_id`/`posted_at` when appended, `head_before`, `head_after`, `ledger_sequence`, `decision` reference. Append-only; the current outcome of an operation is its latest row. |
| `invocations` | `operation` reference (null for reads), `requested_at`, `principal`, `disposition` (`new`, `replay`, `conflict`, `approval`, `read`), `call_id`. |
| `decisions` | `invocation` reference, `operation` reference, canonical serialized `PolicyContext` including the aggregate values read, policy set version, decision, matched rule, reason, `approval` reference. Owned by the invocation, so an operation that needed approval has two. |
| `approvals` | An approval artefact as presented: approver, scope, expiry, digest. |
| `approval_consumptions` | `approval` (`UNIQUE`), the `decision` that consumed it. The `UNIQUE` is what makes an approval single-use; a decision merely *claiming* consumption would not. |
| `events` | Boundary events: inbound `tool_call` and message content, and the data needed to derive the outbound `tool_result` exactly, keyed to their invocation. |
| `reads` | For read tools: the `journal_sequence` and head the projection was at when the read was served, and the result digest. |

**Outcome transitions.** `awaiting_approval` is the only non-terminal outcome. A later
invocation of the same key and fingerprint that presents an approval writes a new
decision, consumes the approval (or fails on the `UNIQUE`), and appends a new outcome row
for the same operation. Nothing is rewritten; the operation's history is its outcome rows
in journal order. The approval is part of the `PolicyContext`, not of the fingerprint, so
the retry matches.

**Transaction protocol, write tools.** One invocation, one SQLite transaction,
`BEGIN IMMEDIATE`. SQLite serializes write transactions; exactly one is active at a time,
whatever the process count.

1. Take the write lock.
2. Confirm the projection's cursor equals the journal's max `journal_sequence`; if not,
   rebuild from `definition` and all outcome rows in order. An appended approval outcome
   advances the max like any other row, so a stale process cannot pass this step. The
   entry-chain head is checked against the rebuilt projection as an integrity test; it is
   not the cursor, because lifecycle commands leave it unchanged.
3. Write the inbound `events` row (the `tool_call` as received).
4. Look up `key`.
   - Absent: write `invocations` (`new`), continue to step 5.
   - Present, fingerprint matches, latest outcome terminal: write `invocations`
     (`replay`), write the outbound `events` row derived from the stored outcome, commit,
     return. **No decision row**: no policy evaluation happened, and the trace says so
     (Decision 4).
   - Present, fingerprint matches, latest outcome `awaiting_approval`, approval presented:
     write `invocations` (`approval`), continue to step 5.
   - Present, fingerprint differs: write `invocations` (`conflict`), outbound `events`,
     commit, return. No decision row.
5. Build the `PolicyContext`, reading aggregates from `outcomes` and `decisions` inside
   this transaction. Consume the approval if one is presented: insert into
   `approval_consumptions`; a `UNIQUE` violation is a denial with reason
   `approval_already_used`. Write the `decisions` row. If the decision is not `allow`,
   append an outcome (`denied` or `awaiting_approval`), write the outbound event, commit,
   return.
6. Run the command through the pure core. Deterministic and cheap.
7. Append the outcome (`applied` or `rejected`, with effects and heads). Write the
   outbound `events` row with the data the response will be rendered from. Commit.
8. Only now render and return the response.

A crash before commit leaves nothing: no key, no decision, no consumed approval, no
events. A retry runs afresh. A crash after commit but before step 8 leaves a complete
invocation including its outbound event; the retry hits step 4 as a `replay`. At no point
does a key exist without its outcome, an approval get consumed twice, or a `tool_call`
exist without the data for its `tool_result`.

**Transaction protocol, read tools** (`balance`, `trial_balance`). Reads are not
operations and never enter the rebuild. One invocation, one deferred read transaction
under WAL, which gives a consistent snapshot: write `events` (inbound), `invocations`
(`read`), a `decisions` row if the read is policy-gated, then serve from a projection
confirmed at the snapshot's max `journal_sequence`, and write `reads` with that sequence,
the head, and the result digest, plus the outbound event. A read's trace records which
journal position it observed, so a reviewer can tell a stale answer from a wrong one.

**Trace derivation.** `trace(journal) -> Trace` is deterministic: rows in
`journal_sequence` order, event identity `(journal_sequence, kind)`. There is no consumer
offset because there is no queue. The M2a replayer, run on the derived trace, checks the
journal against its own projection.

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

*(Amended: where the context is stored, and who owns it.)* The full canonical
`PolicyContext`, not a summary, is written to the `decisions` table in the same
transaction, including the aggregate values the velocity rules read and the approval
reference if one was consumed. A decision belongs to the *invocation* that evaluated
policy, not to the operation: an operation that needed approval has two decisions, one
per evaluating invocation, and a replay has none. Approval single-use is enforced by
`approval_consumptions.approval UNIQUE`, not by a flag inside the context. Replaying a
decision needs no live state; everything it evaluated is in the row.

## Decision 3: the runtime surface is a local MCP server (M4)

*(Amended: the first version promised "any MCP client" a tool that "cannot exceed its
mandate" while leaving authentication out of scope. Without an authenticated principal
there is no mandate to enforce, so the claim did not hold. The scope is now explicit.)*

`ledgergate serve` exposes the ledger as Model Context Protocol tools over **stdio only**.
Write tools (`open_transaction`, `authorize`, `settle`, `refund`, `reverse`) require an
idempotency key and run the M2b write protocol. Read tools (`balance`, `trial_balance`)
run the read protocol against a snapshot and record the journal position they observed.
Every call is checked, recorded and durable before it returns, and every call and outcome
is derivable as a trace event from the journal, in one order.

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

## Decision 4: trace schema v2 is built around intents and dispositions (M3)

*(Amended three times. Revision 2 said every invocation emits a `policy_decision`. A replay
or a conflict never evaluates policy, and synthesizing a decision that did not happen
would be a lie in the trace. Reads had no shape at all.)*

Schema v1 is frozen as published. M3 publishes **schema v2**, in which the unit is an
**intent** (a proposed command, identified before anything decides on it) and every
intent has a **disposition** saying what the runtime did with it.

```
tool_call
  command_intent          intent_id, command, context digest       exactly one per write invocation
  invocation_resolution   intent_id, disposition, operation ref    exactly one per intent
                          disposition: new | replay | conflict | approval
  [policy_decision]       intent_id, decision, rule, version       iff disposition in {new, approval}
  [ledger_command         intent_id                                iff a policy_decision == allow
   ledger_result]         command_id                               iff ledger_command
tool_result

tool_call
  read_intent             intent_id, read kind, parameters         exactly one per read invocation
  [policy_decision]                                                iff the read is policy-gated
  read_result             intent_id, journal position observed, head, result digest
tool_result
```

Cardinality and order are rules of the schema description, enforced by the models as v1's
are. A `replay` or `conflict` intent has no decision and no ledger pair; it references the
operation it resolved to, whose original decision and pair appear earlier in the same
trace. A `deny` or `approval_required` intent ends at its decision and replays by policy
alone. An `allow` intent continues into the v1-style pair and replays by both policy and
ledger. An `approval` intent carries the approval reference in its decision and, if
allowed, its own ledger pair.

A v1 document maps into v2 by giving each `ledger_command` an intent with disposition
`new` and a decision of `allow` under policy version `none`; that is how the v1 replayer's
guarantees carry over. The runtime reads v1 and v2 and writes v2.

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
   `trace_id`, subject identifiers in the `PolicyContext`): deterministically tokenized
   with the same keyed HMAC at admission, **on every reference**, before anything else
   reads them: before the `PolicyContext` is built, before the command is fingerprinted,
   before the ledger looks anything up, before any row is written. `open_transaction`
   stores the token of a `transaction_id`; a later `settle` with the same raw id tokenizes
   to the same value and finds it. A retry with the raw key tokenizes to the same key and
   hits the same operation. Replay operates only on stored tokens and needs no key. The
   token domain and key version are in `definition`; rotating the key means a new
   journal, and cross-journal correlation is an explicit operation, not an accident.
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
| M2b | Append-only journal: definition, operations, outcomes, invocations, decisions, approvals, consumptions, events, reads; one global sequence; write and read protocols; deterministic trace derivation |
| M2c | Redaction and identifier tokenization at admission; token domain in the definition; redacted traces replay |
| M3 | Trace schema v2 around intents and dispositions; `PolicyContext` persisted per evaluating invocation; single-use approvals; policy layer with in-transaction velocity state; invariant registry; scorecard; `ledgergate verify` |
| M4 | `ledgergate serve`: stdio MCP, single local principal, log protocol on every call |
| M5 | OpenTelemetry GenAI observational adapter with completeness validation; thin wrappers; cassettes |
| M6 | Scenario corpus and red-team corpus; SARIF/JUnit; drift table across model versions |
| M7 | Mutation gate, CodeQL, OpenSSF Scorecard, PyPI release, conformance levels |
| M8 | Authenticated network transport and principals; approval artefacts with real approvers; external execution via outbox and reconciliation |

## Consequences

- The journal is the one durable truth and is strictly append-only; there is no row
  anywhere that is ever updated. Traces are a deterministic function of it. The M2a
  replayer, run on the derived trace, checks a journal against its own projection.
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
