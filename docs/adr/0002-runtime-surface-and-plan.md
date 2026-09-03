# ADR 0002: A runtime surface, a durable command log, and an authority layer

- Status: Accepted
- Date: 2026-09-03
- Amended: 2026-09-03 (four times), after review found first that the guarantees lacked
  mechanisms, then that the mechanisms lacked invariants, then that one mutable row
  undermined the cursor built to protect it, then that the protocol never said where the
  operation row is inserted and that approvals were consumed before being validated.
  Amendments are marked.

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

*(Amended four times. The fourth review found: the write protocol never inserted the
`operations` row that every other row references; the read protocol tried to write
inside a deferred read transaction, which SQLite cannot promise; approvals were consumed
before being validated or bound to anything; the attempted command of a conflict was not
stored; and the token format was unspecified while the ledger enforces a 256-character,
single-line identifier. All fixed below.)*

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
from a single monotonic counter. That is the projection cursor and the only ordering
source. Trace event order is derived from it (see Trace derivation). Per-table sequences
do not exist.

| Table | Holds |
| :--- | :--- |
| `definition` | Written once: chart, currency registry, codec version, policy set version, identifier token domain and key version, approval verification key. Free text in the definition (account names) passes the same redactor as everything else. A journal is bound to one definition; changing it means a new journal. |
| `operations` | `key` (tokenized, `UNIQUE`), `fingerprint`, canonical `command`. Immutable identity only. |
| `outcomes` | `operation` reference, `previous_outcome`, `outcome` (`applied`, `rejected`, `denied`, `awaiting_approval`), error type and message, `entry_id`/`posted_at` when appended, `head_before`, `head_after`, `ledger_sequence`, `decision` reference. Append-only; the current outcome of an operation is its latest row. |
| `invocations` | `operation` reference (null for reads and invalid calls), `requested_at`, `principal`, `disposition` (`new`, `replay`, `conflict`, `approval`, `read`, `invalid`), `attempted_fingerprint`, `attempted_command` (what *this* attempt asked, so a conflict shows both sides), `call_id`. |
| `decisions` | `invocation` reference, `operation` reference, canonical serialized `PolicyContext` including the aggregate values read, policy set version, decision, matched rule, reason, `approval` reference. Owned by the invocation, so an operation that needed approval has two. |
| `approvals` | An approval artefact as presented and *as validated*: `approval_id`, approver principal, bound operation `fingerprint`, bound `key`, bound subject, bound amount and currency, `issued_at`, `expires_at`, signature, and the validation verdict. |
| `approval_consumptions` | `approval` (`UNIQUE`), the `decision` that consumed it. The `UNIQUE` is what makes an approval single-use; a decision merely *claiming* consumption would not. |
| `events` | Boundary events: the inbound `tool_call` (tool, admitted arguments after redaction, `call_id`) or message, and the outbound `tool_result` data (ok, result or error) from which the response is rendered, keyed to their invocation. |
| `reads` | For read tools: the `journal_sequence` and head the projection was at when served, and the result digest. |

**Outcome transitions.** `awaiting_approval` is the only non-terminal outcome. A later
invocation of the same key and fingerprint that presents a *valid, bound* approval writes a
new decision, consumes the approval (or fails on the `UNIQUE`), and appends a new outcome
row for the same operation. Nothing is rewritten; the operation's history is its outcome
rows in journal order. The approval is part of the `PolicyContext`, not of the fingerprint,
so the retry matches.

**Approval artefacts.** An approval is issued out of band (in M4, by the operator via
`ledgergate approve`, signed with a key whose verification counterpart is in
`definition`). It binds to exactly one pending operation: its `fingerprint`, tokenized
`key`, subject, amount and currency. Validation happens *before* the approval is placed in
the `PolicyContext` and before anything is consumed: signature verifies against the
definition's key; `expires_at` is after the injected evaluation time; every bound field
equals the pending operation's. A failed validation is a decision of `deny` with the
reason (`approval_invalid`, `approval_expired`, `approval_scope_mismatch`) and **nothing is
consumed**. Only an approval that validated and whose policy evaluation returned `allow`
is consumed, inside the same transaction.

**Write protocol.** One invocation, one SQLite transaction, `BEGIN IMMEDIATE`. SQLite
serializes write transactions; exactly one is active at a time, whatever the process
count.

1. Take the write lock.
2. Confirm the projection's cursor equals the journal's max `journal_sequence`; if not,
   rebuild from `definition` and all outcome rows in order. An appended approval outcome
   advances the max like any other row, so a stale process cannot pass this step. The
   entry-chain head is checked against the rebuilt projection as an integrity test; it is
   not the cursor, because lifecycle commands leave it unchanged.
3. Admit the request: tokenize every caller identifier (Decision 5), redact free text,
   decode the command. If admission fails (unknown tool, malformed arguments, identifier
   that fails validation after tokenization), write the inbound `events` row, an
   `invocations` row with disposition `invalid`, and the outbound `events` row carrying
   the error; commit; return. No operation exists for an invalid call, and the trace
   shows the call and its failure.
4. Write the inbound `events` row (the admitted `tool_call`).
5. Look up the tokenized `key` in `operations`.
   - Absent: **insert the `operations` row** (`key`, `fingerprint`, canonical `command`),
     then the `invocations` row (`new`, referencing it). Continue to step 6.
   - Present, fingerprint matches, latest outcome terminal: write `invocations`
     (`replay`), write the outbound `events` row derived from the stored outcome, commit,
     return. **No decision row**: no policy evaluation happened.
   - Present, fingerprint matches, latest outcome `awaiting_approval`, approval presented:
     validate the approval as above; write `invocations` (`approval`); continue to step 6
     with the validated (or failed) approval in the context.
   - Present, fingerprint matches, latest outcome `awaiting_approval`, no approval:
     `replay` of the `awaiting_approval` outcome.
   - Present, fingerprint differs: write `invocations` (`conflict`, with the attempted
     fingerprint and command), outbound `events`, commit, return. No decision row.
6. Build the `PolicyContext`, reading aggregates from `outcomes` and `decisions` inside
   this transaction. Evaluate. Write the `decisions` row. If the decision is `allow` and
   an approval was presented, insert into `approval_consumptions`; a `UNIQUE` violation
   converts the decision to `deny` with reason `approval_already_used` (the decision row
   is written with that final verdict). If the decision is not `allow`, append an outcome
   (`denied` or `awaiting_approval`), write the outbound event, commit, return.
7. Run the command through the pure core. Deterministic and cheap.
8. Append the outcome (`applied` or `rejected`, with effects and heads). Write the
   outbound `events` row with the data the response will be rendered from. Commit.
9. Only now render and return the response.

A crash before commit leaves nothing: no operation, no key, no decision, no consumed
approval, no events. A retry runs afresh. A crash after commit but before step 9 leaves a
complete invocation including its outbound event; the retry hits step 5 as a `replay`. At
no point does an operation exist without an outcome, an approval get consumed twice, or a
`tool_call` exist in the journal without the data for its `tool_result`.

**Failures the journal cannot record.** If the transaction itself cannot complete
(`SQLITE_BUSY` past the retry budget, a constraint violation other than the approval
`UNIQUE`, an integrity failure in step 2, or an exception from the core that is not a
`LedgerError`, which is a bug), the transaction is rolled back, nothing is written, and
the caller receives an MCP error. This is the one class of call that leaves no journal
row, and it is stated rather than hidden: the journal was unavailable, so it could not be
the record.

**Read protocol** (`balance`, `trial_balance`). Reads are not operations and never enter
the rebuild, but an *audited* read is recorded, and recording is a write. It therefore
runs under `BEGIN IMMEDIATE` like everything else and accepts serialization with writers;
balance queries are cheap and the alternative (a deferred transaction that later upgrades
to write) can fail with `SQLITE_BUSY` after the snapshot is taken, leaving a result that
no longer matches any recordable state. Steps: lock; confirm cursor (step 2); admit (step
3); write inbound `events` and `invocations` (`read`); write a `decisions` row if the read
is policy-gated; serve from the projection; write `reads` with the cursor, head and result
digest, and the outbound `events` row; commit; respond. Unaudited reads of the projection
by the process itself are ordinary snapshot reads and write nothing.

**Trace derivation.** `trace(journal) -> Trace` is deterministic and emits **schema v2
only** (Decision 4). Rows are visited in `journal_sequence` order; each row yields zero or
more v2 events in a fixed intra-row ordinal; `seq` is the dense enumeration of emitted
events in `(journal_sequence, ordinal)` order. Identifiers are taken from the journal:
`call_id` from `events`, `intent_id` as the invocation's `journal_sequence`, `command_id`
as the operation's `journal_sequence`, all rendered as identifiers. The top-level `chart`
and `currencies` come from `definition`. A trace is derived from a **whole journal**;
windowed export is a later concern and, when it exists, a reference to an operation
outside the window is permitted and marked as external rather than fabricated. The v2
replayer (M3) checks a journal against its own projection. The v1 replayer is not
involved: v1 is the offline ingest format, and v1 documents are lifted into the v2 model
on read (Decision 4).

**Concurrency.** Any number of unaudited readers under WAL; every journal write,
including audited reads, is a serialized `BEGIN IMMEDIATE` transaction. Multiple writer
processes are correct under step 2 and not optimized.

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
                          disposition: new | replay | conflict | approval | invalid
                          attempted command digest (so a conflict shows what was tried)
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
operation it resolved to by that operation's identifier. Because a trace is derived from a
whole journal, the original decision and pair appear earlier in the same trace; in a
windowed export the reference is marked external. An `invalid` intent has neither
operation nor decision and ends at its `tool_result` error. A `deny` or `approval_required` intent ends at its decision and replays by policy
alone. An `allow` intent continues into the v1-style pair and replays by both policy and
ledger. An `approval` intent carries the approval reference in its decision and, if
allowed, its own ledger pair.

A v1 document is lifted into the v2 model on read by giving each `ledger_command` an
intent with disposition `new` and a decision of `allow` under policy version `none`. The
runtime reads v1 and v2, derives v2 from the journal, and never derives v1: the journal
has more structure than v1 can carry, and inventing a lossy projection would create a
second thing to keep consistent.

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
   hits the same operation. Replay operates only on stored tokens and needs no key.

   The token format is fixed so it always satisfies the ledger's own identifier rule
   (non-empty, single line, at most 256 characters): the raw value is first validated by
   `require_identifier`, then the token is `tk1_<domain>_<base64url(HMAC-SHA256(key,
   domain || raw))>` with no padding, 43 characters of digest, a domain of at most 32
   `[a-z0-9-]` characters, and a fixed `tk1` version prefix. The result is at most 80
   characters of `[A-Za-z0-9_-]` and is validated once more after construction. The
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
| M2b | Append-only journal: definition, operations, outcomes, invocations, decisions, approvals, consumptions, events, reads; one global sequence; write and audited-read protocols; approval validation before consumption; deterministic v2 derivation |
| M2c | Redaction and identifier tokenization at admission; token domain in the definition; redacted traces replay |
| M3 | Trace schema v2 around intents and dispositions; `PolicyContext` persisted per evaluating invocation; single-use approvals; policy layer with in-transaction velocity state; invariant registry; scorecard; `ledgergate verify` |
| M4 | `ledgergate serve`: stdio MCP, single local principal, log protocol on every call |
| M5 | OpenTelemetry GenAI observational adapter with completeness validation; thin wrappers; cassettes |
| M6 | Scenario corpus and red-team corpus; SARIF/JUnit; drift table across model versions |
| M7 | Mutation gate, CodeQL, OpenSSF Scorecard, PyPI release, conformance levels |
| M8 | Authenticated network transport and principals; approval artefacts with real approvers; external execution via outbox and reconciliation |

## Consequences

- The journal is the one durable truth and is strictly append-only; there is no row
  anywhere that is ever updated. Traces (schema v2) are a deterministic function of it.
  The v2 replayer, run on the derived trace, checks a journal against its own projection;
  the v1 replayer keeps checking offline v1 documents, lifted into the v2 model.
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
