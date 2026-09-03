# ADR 0002: A runtime surface, a durable command log, and an authority layer

- Status: Accepted
- Date: 2026-09-03
- Amended: 2026-09-03, after review found the first version made guarantees its
  decisions did not support. The amendments are marked in each section.

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

## Decision 1: the ledger persists as an append-only command log (M2b)

*(Amended: the first version said "one row per command plus UNIQUE(key)" and called
restart-safe idempotency a consequence. That is the mechanism, not the protocol. This
section specifies the protocol.)*

**Authority.** The command log is the single authoritative artefact. The in-memory ledger
is a projection rebuilt by replaying it. Trace events are *derived* from the log (an
outbox), never written independently, so the two cannot disagree; a trace produced from a
log is a view of it.

**What one row holds.** Everything replay needs and nothing else can supply:

| Column | Why |
| :--- | :--- |
| `sequence` | Position; the primary key |
| `key` | Idempotency key, `UNIQUE` |
| `fingerprint` | Canonical request digest, to tell a replay from a conflict |
| `command` | Canonical encoded command, schema-versioned |
| `outcome` | `applied`, `replayed`, or `rejected` with the error type and message |
| `entry_id`, `posted_at` | The effects the ledger consumed, when it appended |
| `head_before`, `head_after`, `ledger_sequence` | To detect a projection that diverged |
| `policy_version`, `decision` | From M3; null before |

**Ledger definition.** A separate `definition` table holds, once per log: the chart of
accounts, the currency registry with exponents, the command codec version, and the
policy set version. It is written when the log is created and never updated. Changing
any of these means a new log. A log therefore replays the same way regardless of what
the deployment's configuration says today.

**Transaction protocol.** One command, one SQLite transaction, `BEGIN IMMEDIATE`:

1. Take the write lock. SQLite serializes writers; there is exactly one at a time.
2. Look up `key`. If present and `fingerprint` matches, return the stored outcome without
   touching anything. If present and it differs, return a conflict. Because the row is
   written only on commit, there is no "in progress" state to observe: a key is either
   fully recorded or absent.
3. Confirm the projection is at `head_after` of the last row; if not, rebuild it from the
   log before continuing. This is what makes a second process safe: it cannot apply a
   command against a stale view.
4. Run the command through the pure core (and, from M3, through policy). This is
   deterministic and cheap.
5. Insert the row with the outcome and effects. Commit.
6. Only now return the result to the caller.

A crash before step 5 commits leaves nothing: no key claimed, no result, no trace. The
caller retries with the same key and the command runs afresh. A crash after commit but
before step 6 leaves a committed row; the caller's retry hits step 2 and receives the
stored outcome. In neither case does the ledger apply twice, and in neither case does a
key exist without its complete result. Rejected commands are rows too, so a replayed
rejection returns the same rejection.

**Concurrency.** M2b supports one writer process at a time, enforced by the SQLite lock,
with any number of readers under WAL. Multiple writer processes are correct under this
protocol (step 3) but not optimized; that is a later concern.

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

A decision is `allow`, `deny`, or `approval_required`, with the matched rule. Policies
stay pure: given the same context they return the same decision, so offline evaluation
over a trace and online evaluation at the boundary run the same code on the same inputs.

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

## Decision 4: trace schema v2 carries policy decisions (M3)

*(Amended: the first version said policy violations are "first-class outcomes in the
trace". Schema v1 has closed event variants and no policy event, so that required a
versioning decision it did not make.)*

Schema v1 is frozen as published. M3 publishes **schema v2**, which adds a
`policy_decision` event (policy set version, decision, matched rule, context digest,
approval reference) and links it to the command it judged by `command_id`. The runtime
reads v1 and v2 and writes v2. The v1 replayer keeps working on v1 documents; a v2
document with policy events replays policy as well as ledger.

## Decision 5: redaction happens at admission, over v1 fields only (M2c)

*(Amended: the first version promised "redacted traces still replay" without saying how.
`EntryDraft.description` is inside the entry digest; redacting a recorded trace afterwards
would change the digest and break its own replay.)*

Free-text fields (`description`, message `content`, tool `arguments` and `result`, tag
values) are redacted **before the ledger sees them**: the runtime and recorder apply the
redactor at the admission boundary, so every digest is computed over already-redacted
text and a trace replays exactly. Identifiers, amounts, currencies, account ids and keys
are never redacted; they are structural, and in this model they are not personal data.
Redaction is fail-closed: a field not on the allowlist is redacted. Tokens are
deterministic (keyed HMAC), so equal inputs redact equally across runs. M2c covers schema
v1 fields; v2's policy fields are designed redaction-aware in M3.

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
| M2b | Command log with the protocol above; ledger definition table; ledger as projection; trace as outbox |
| M2c | Fail-closed redaction at admission over v1 fields; deterministic tokens; redacted traces replay |
| M3 | Trace schema v2 with `policy_decision`; `PolicyContext`; policy layer with in-transaction velocity state; invariant registry; scorecard; `ledgergate verify` |
| M4 | `ledgergate serve`: stdio MCP, single local principal, log protocol on every call |
| M5 | OpenTelemetry GenAI observational adapter with completeness validation; thin wrappers; cassettes |
| M6 | Scenario corpus and red-team corpus; SARIF/JUnit; drift table across model versions |
| M7 | Mutation gate, CodeQL, OpenSSF Scorecard, PyPI release, conformance levels |
| M8 | Authenticated network transport and principals; approval artefacts with real approvers; external execution via outbox and reconciliation |

## Consequences

- The command log is the one durable truth. Traces are views of it. The M2a replayer
  becomes the consistency check between a log and any trace claimed to derive from it.
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
