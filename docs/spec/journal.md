# Spec: the journal

Normative specification for the M2b persistence layer decided in
[ADR-0002](../adr/0002-runtime-surface-and-plan.md). The ADR says *what* and *why*; this
document says *exactly how*, and is the text an implementation and its tests are held to.

## Terms

- **Operation**: the immutable identity of an idempotency key: what was asked, once.
- **Outcome**: a fact about an operation at a point in time. An operation has one or more,
  appended, never edited. Its *current* outcome is its latest.
- **Invocation**: one caller attempt. A key may see many.
- **Decision**: one policy evaluation, owned by the invocation that triggered it.
- **Projection**: the in-memory `Ledger`, rebuilt from outcomes. Never authoritative.

`replayed` and `conflict` are properties of invocations, never of operations. The ledger
rebuilds from outcomes; the trace derives from invocations and events.

**Where the journal deliberately differs from the in-memory core.** The core
(`ledgergate.ledger.state`) records an operation only on success; a rejected command
raises and leaves its idempotency map untouched, so the same key can be retried. The
journal records `rejected` as a terminal outcome: the key is spent, and a retry replays
the rejection. This is the ledger-of-record reading of "same key, same result": a caller
who wants to try again after `InsufficientFundsError` uses a new key, and the journal
shows both attempts. The README's idempotency row states this. The operation fingerprint is the core's, computed by one exported function. Today the
core's `fingerprint(kind, payload)` primitive is called with five inlined payload layouts
inside `Ledger` (`state.py`); M2b lifts those into a public
`ledgergate.ledger.command_fingerprint(command) -> str` that `Ledger.execute` and the
journal both call, so there is one definition and `operations.fingerprint` equals what the
core would compute. `PolicyContext.command_digest` is `operations.fingerprint` for a write intent and
`invocations.request_digest` for a read intent, and the context carries `digest_kind`
(`fingerprint` or `request`) so a consumer recomputing a decision knows which to
recompute. There is no third digest.

**Admission input and Request.** The transport hands admission one untyped JSON value
(M4's MCP layer decodes the wire and passes the params object; the journal never sees
wire bytes). Admission's *output* on success is a canonical `Request`: `tool`, `arguments`
(JSON object), `call_id`, `principal`, `key` (idempotency key), optional `approval`. Two
named digests, both SHA-256 over canonical JSON (sorted keys, no whitespace, UTF-8):
`input_digest`, over the untyped input, is what the failure envelope records, because a
malformed input has no `Request` to digest; `request_digest`, over the `Request`, is
recorded on every admitted invocation. Neither is the operation fingerprint, defined under *Terms*, which is over the decoded *command*. "Canonical JSON" means RFC 8785 (JSON
Canonicalization Scheme): UTF-16 code-unit key order, ECMAScript number formatting, no
whitespace, UTF-8. Under JCS `5.0` and `5` are the same number and serialize as `5`; the
runtime's refusal of whole floats where an integer is required is model validation that
runs *before* digesting, not a property of the digest. Python's `json.dumps` is not JCS
(it emits `5.0` and `1e+16`, and sorts keys by code point), so M2b implements or vendors a
JCS serializer and tests it against the RFC 8785 appendix vectors.

JCS numbers are IEEE-754 doubles, so integers beyond 2^53 lose identity and huge ones have
no serialization at all. The admission input is therefore **I-JSON (RFC 7493) by
contract**: every number is an integer in `[-(2^53-1), 2^53-1]` or a finite double, and
every string is a sequence of Unicode scalar values (no unpaired surrogates). The transport
enforces this at decode (`json.loads` with `parse_int`, `parse_float` **and
`parse_constant`** hooks; `parse_float` rejects anything that is not finite after
conversion (`1e400` overflows to infinity without ever reaching `parse_constant`), and
`parse_constant` rejects the `NaN`/`Infinity` literals unconditionally; plus a
post-decode surrogate scan) *before* admission; a violation is a transport error with no
journal row, listed under *Failures the journal cannot record*. M2b has no transport, so
the JCS serializer itself is the enforcer of last resort: it raises on any contract
violation, and that raise is the same unrecorded-failure class. The codec itself imposes
no amount bound: its output is a storage form that nothing JCS-digests, the transport's
I-JSON contract already bounds every amount a runtime command can carry, and the frozen v1
trace path (`dump_trace`, `json.dumps`) must keep accepting any integer the schema accepts.
The artefact's `amount` display field, which *is* JCS-signed, is a decimal string like
every other digested amount (next paragraph). The JCS serializer lives in
`ledgergate.codec`, the one layer both `journal` and M3's `approve`/`derive` may import.

**Digests over values the core produces.** Balances, trial balances and policy aggregates
are *sums* of bounded amounts and are not themselves bounded (`Money.amount` is an
unbounded `int`). Any JCS-digested structure that contains a Money amount therefore
serializes that amount as a **decimal string**, exactly as the core's own fingerprint
already does (`str(amount)`); this applies to `reads.result_digest` and to the aggregate
values inside a serialized `PolicyContext`. It also applies to **tool results as returned
and as stored**: every Money amount in a `balance` or `trial_balance` result is a decimal
string in the outbound `events` row and on the wire, so `reads.result_digest` is simply
SHA-256 over the JCS serialization of the stored result, with no transformation a consumer
would have to guess, and a JavaScript client never sees an integer it cannot represent.

## Sequences

Three distinct things, kept distinct because conflating them was a review finding:

- **`journal_sequence`**: every row in every fact table carries one, from a single
  monotonic counter. It orders every durable fact. Per-table sequences do not exist.
  SQLite has no cross-table sequence, so the counter is a table:
  `journal (journal_sequence INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL)`; `AUTOINCREMENT` makes strictly-increasing a database mechanism rather than a consequence of never deleting. Each write
  protocol step that creates a fact row first inserts a `journal` row naming the fact
  table, then inserts the fact row with that value as its own `PRIMARY KEY`, which is
  also a foreign key to `journal`. Committed values are strictly increasing. Gaps, if any
  arise, are permitted (trace `seq` is a dense re-enumeration and never exposes them).
  `kind` is enforced: each fact table has a `BEFORE INSERT` trigger asserting that its
  `journal_sequence` row has the matching `kind`, so an allocation cannot be consumed by
  the wrong table.
- **Projection cursor**: the `journal_sequence` of the latest **outcome** row folded into
  the projection, or `0` for a projection into which nothing has been folded. Only
  outcomes change the ledger; invocations, events, decisions, approvals and reads advance
  `journal_sequence` without touching it. Freshness is therefore "is my cursor equal to
  `COALESCE(MAX(outcomes.journal_sequence), 0)`", not to the global maximum, which is
  always ahead of any projection. `journal_sequence` starts at 1, so `0` is never a row.
- **Trace order**: derived, not stored; defined in [trace-v2](trace-v2.md). It anchors
  every event of an invocation to that invocation's `journal_sequence`, because the
  foreign keys force the invocation row to be written *before* the inbound event that
  conceptually precedes it.

## Tables

All strictly append-only. No row is ever updated or deleted.

| Table | Holds |
| :--- | :--- |
| `definition` | Written once: `journal_id` (128 random bits generated at creation, never derived from anything reusable), chart, currency registry, codec version, policy set version, identifier token domain and key version, approval verification key. Free text in the definition (account names) passes the same redactor as everything else. A journal is bound to one definition; changing it means a new journal. |
| `operations` | `key` (tokenized, `UNIQUE`), `fingerprint`, `command` as encoded by the codec (a storage form, not a digest input; identity is `fingerprint`). |
| `outcomes` | `operation`, `previous_outcome` (null for the first outcome of an operation, else the operation's latest outcome at the time of appending), `outcome` (`applied`, `rejected`, `denied`, `awaiting_approval`), error type and message, `entry_id`/`posted_at` when appended, `head_before`, `head_after`, `ledger_sequence`, `decision`. The chain constraints are in *Outcome chain*. |
| `invocations` | `operation` (null for reads and invalid calls), `requested_at`, `principal`, `disposition` (`new`, `replay`, `conflict`, `approval`, `read`, `invalid`), `attempted_fingerprint`, `attempted_command` (what *this* attempt asked, so a conflict shows both sides), `request_digest` (null for `invalid`, which has `input_digest` in its envelope instead), `call_id`. |
| `invocation_responses` | `invocation` (`UNIQUE`), `outcome` (the exact outcome row this invocation's response was rendered from), `disposition` (copied from the invocation row at insert; an intra-row `CHECK` requires `outcome` non-null iff `disposition IN ('new','replay','approval')`, and a `BEFORE INSERT` trigger rejects a row whose `disposition` differs from its invocation's, since SQLite `CHECK` cannot look at another table), `response` (the disposition-level result: `applied`, `rejected`, `denied`, `awaiting_approval`, `replayed`, `conflict`, `invalid`, `read`; the `tool_result` error type, such as `ApprovalRejected` or `PolicyDenied`, lives in the outbound `events` row, so `response` is what happened to the operation and the event is what the caller was told). Written after the outcome it names exists, so a `new` invocation's response row follows its first outcome. This is what binds a replay to the outcome that answered it *at the time*, rather than to whatever the operation's current outcome is when the journal is later read. |
| `decisions` | `invocation`, `operation`, canonical serialized `PolicyContext` including the aggregate values read, policy set version, decision, matched rule, reason, the approval presentation row considered, its `approval_verdict` (`approval_valid`, `approval_already_used`, `approval_not_applicable`, or the failing check's result `approval_invalid` / `approval_expired` / `approval_scope_mismatch`; null when no artefact was presented), the `presentation` reference (non-null whenever any presentation row exists for this invocation), and the `consumption` row if check 4 succeeded. |
| `approvals` | One row per *presentation* of an artefact (a presentation row's identity is its `journal_sequence`), referencing the presenting `invocation`: the `journal_id` *as presented* (so a foreign one is recoverable from the row), the logical `approval_id` from the artefact, approver principal, bound `fingerprint`, bound tokenized `key`, bound subject, bound amount and currency, `issued_at`, `expires_at`, signature, and the **check result** of the pure checks 1 to 3 (`checks_passed`, `approval_invalid`, `approval_expired`, `approval_scope_mismatch`, or `approval_not_applicable`). The *final verdict*, which also depends on check 4 (consumption), lives on the `decisions` row that considered this presentation; a row written before a check cannot carry that check's result. Presenting the same artefact twice appends two rows. |
| `approval_consumptions` | logical `approval_id` (`UNIQUE`), the presentation row, the consuming `invocation`. The `UNIQUE` is on the logical id, so an artefact is consumable once however many times it is presented. The decision that used it references this row, not the reverse: the row is written during validation, before the decision exists. |
| `events` | Boundary events, each with a nullable `invocation` (null only for standalone `message` rows, which are written by their own transaction, allocator row included, and belong to no invocation): the inbound `tool_call` (tool, admitted arguments after redaction, `call_id`), or the failure envelope written by admission step 3, or a message; and the outbound `tool_result` data the response is rendered from (`ok=true` with result, or `ok=false` with error type and message; a policy denial is `ok=false`, type `PolicyDenied`, message the rule and reason), keyed to their invocation. |
| `reads` | For read tools: the `journal_sequence` and head the projection was at when served, and the result digest. |

## Invariants

1. Every row a response depends on is committed before the response is rendered.
2. Every operation has at least one outcome in the same transaction that created it.
3. An approval is consumed at most once *per artefact*: `approval_consumptions.approval_id
   UNIQUE` within a journal, and the signed `journal_id` binding an artefact to one journal
   across *distinct* journals (a copied journal file is the same journal, so the guarantee
   is per journal, not per file); and only after every validation check passed. A consumed approval's operation
   leaves `awaiting_approval` in the same transaction (to `applied`, `rejected` or
   `denied`), so a consumed artefact never has a live operation to attach to.
4. A `tool_call` row never exists without the data for its `tool_result`.
5. Whenever a command is evaluated against the projection, the projection's cursor
   equals `COALESCE(MAX(outcomes.journal_sequence), 0)` as of the start of the transaction. Rows this
   transaction writes before the core runs (invocation, events, decision) are not
   outcomes and do not move it; the outcome this transaction appends becomes the new
   cursor on commit.
6. Every row is written after every row it references (immediate foreign keys); the
   protocols' step order is the only legal one.
7. Every invocation has exactly one `invocation_responses` row, and for `new`, `replay`
   and `approval` dispositions it names the exact outcome the response was rendered from.
8. Each operation's outcomes form a single chain: one root, no forks, every successor
   references the operation's latest outcome at the time it was appended, and only
   `awaiting_approval` has successors (every other outcome is terminal). See *Outcome
   chain*.

## Outcome chain

`outcomes.previous_outcome` is not decoration; the projection rebuild and the approval
history both depend on it being unambiguous. Four structural rules are enforced by the
schema, and the fifth follows from them:

| Rule | Enforcement |
| :--- | :--- |
| Exactly one root per operation | partial unique index on `operation` where `previous_outcome IS NULL` |
| No forks | `UNIQUE (previous_outcome)` |
| A predecessor belongs to the same operation | composite foreign key `(previous_outcome, operation)` references `outcomes (journal_sequence, operation)`, which requires a `UNIQUE (journal_sequence, operation)` index on `outcomes` (SQLite otherwise reports a foreign-key mismatch) |
| Only `awaiting_approval` has successors | trigger: a row with non-null `previous_outcome` requires the predecessor's `outcome = 'awaiting_approval'` |
| Successor references the latest | implied: one root, same-operation predecessor and no forks make each operation's outcomes a single path containing all of them, and immediate FKs mean a predecessor exists before its successor, so sequence increases along the path. The tip is therefore the latest by construction. The protocol reads that tip under the write lock; rebuild asserts the path property as a self-check. |

Rebuild folds each operation's chain from root to tip in `journal_sequence` order; a chain
that violates any rule above cannot exist in the journal, so rebuild needs no recovery path.

## Approval artefacts

Issued out of band by the operator via `ledgergate approve` (delivered in M3 with the
validation and consumption code; M4 is only the transport that presents artefacts), signed
with a key whose verification counterpart is in `definition`. Binds to exactly one pending operation in exactly one journal, by the
definition's `journal_id` and the operation's `fingerprint` and tokenized `key`; the artefact also
carries subject, amount and currency as display fields for the approver, copied from the
command at issuance and recorded for audit but not compared. Each is nullable, since not
every command has a single amount (`Post`, `Reverse`) and "subject" is defined by the
policy set, not the core; a null is signed as JCS `null`, never as an empty string.

**Validation and consumption**, performed inside the write transaction, after the
invocation row exists (the presentation references it) and *before* the `PolicyContext`
is built. The checks run **in order and short-circuit: the first failure is the result,
and no later check runs**. Checks 1 to 3 are pure and run first; the `approvals`
presentation row is then written carrying their result; check 4 (consumption) runs only if
they all passed and references that row. The final verdict is recorded on the `decisions`
row, which is written after check 4 and so can hold it. An invalid, expired or mis-scoped
artefact never touches `approval_consumptions`.

1. Signature verifies against the definition's key, else verdict `approval_invalid`. The
   signature covers every field the artefact carries (`journal_id`, `approval_id`,
   approver, `fingerprint`, `key`, subject, amount, currency, `issued_at`, `expires_at`),
   serialized per RFC 8785, so no field can be re-labelled after issuance. `journal_id` is
   what makes single use hold *per artefact* rather than per database: a spent artefact
   presented to a successor journal that reuses the same signing key and idempotency keys
   fails check 3, because the
   successor has a different `journal_id`.
2. `expires_at` is after the injected evaluation time, else `approval_expired`.
3. The artefact's `journal_id` equals the definition's and its `fingerprint` and `key` equal the pending operation's, else `approval_scope_mismatch`.
4. **Consumption.** `INSERT INTO approval_consumptions (approval_id, presentation,
   invocation)`. A `UNIQUE` violation means an earlier *committed* transaction consumed
   this logical approval (writes are serialized, so there is no other way). Because writes
   are serialized, the check is exact as a `SELECT` before allocating a `journal` row, so a
   used approval leaves no *consumption* allocator row and no gap (the presentation row and
   its allocator row were already written by design); the `UNIQUE` remains as the
   constraint that makes the `SELECT` merely an optimization. Verdict
   `approval_already_used`. Success: verdict `approval_valid`; the approval is consumed.

The verdict enters the `PolicyContext`. Nothing is consumed on any verdict other than
`approval_valid`.

**Failed verdicts are exactly the outputs of checks 1 to 4 other than `approval_valid`.**
`approval_not_applicable` is not a failed verdict: it records that an artefact was
presented where none was expected, and the disposition's normal path, policy included,
continues.

**On a failed verdict the policy set is not invoked.** The runtime writes the decision
itself: `decision = deny`, `matched_rule = runtime.approval_rejected`, `reason` = the
verdict, `policy_set_version` = the configured set (so the row still says which set
*would* have run). A consumer recomputing from `context` sees the failed verdict and the
`runtime.` rule prefix and knows no policy code was evaluated. This keeps policies pure
functions that never see an unusable approval, and makes the pending-operation table
total.

**A valid approval followed by `approval_required` is a fatal configuration error.** The
rule that asked for approval has been satisfied; a policy set that asks again cannot be
detected statically (policies are code over a context), so it is caught at runtime: the
transaction is rolled back and the failure is reported under *Failures the journal cannot
record*. Nothing is consumed and the operation stays `awaiting_approval`. Fixing the
policy set means a new definition and therefore a new journal; a pending operation does not
migrate. It expires with the retired journal, and the caller resubmits under a new key in
the new one. The retired journal remains the record of what was attempted.

**An artefact presented on any other disposition** (`new`, `replay` of a terminal outcome,
`conflict`, `read`) is not silently dropped: an `approvals` presentation row with check result
`approval_not_applicable` is written immediately after the inbound `events` row (so its
`invocation` reference resolves), nothing is consumed, and the disposition's normal path
continues. On `invalid`, no `Request` was decoded, so there is no artefact to present; any
approval-shaped content is part of the bounded envelope blob. The presentation row is
carried into the v2 trace on `invocation_resolution` as a presentation reference, so the
trace does not drop what the journal kept. Approval is a two-step protocol by design: a request that needs one is
first told so (`awaiting_approval`), and only then is an artefact meaningful.

**Why a validated approval is consumed before policy runs, whatever policy then says.**
The artefact binds to exactly one pending operation, and every decision-to-outcome row for
a valid presentation leaves that operation terminal: `allow` runs the core (`applied` or
`rejected`), `deny` appends `denied`. No path leaves the operation pending after a valid
presentation, so no later attempt could use the artefact, and consuming it unconditionally
is both simpler and exact. A `deny` on a valid approval is recorded as a decision that
consumed the approval and refused anyway, with the refusing rule as the reason. That is
the audit fact.

**Decision-to-outcome for a new operation** (first evaluation, disposition `new`):

| Policy decision | First outcome | `tool_result` |
| :--- | :--- | :--- |
| `allow` | `applied` or `rejected`, from the core | per the core's result |
| `deny` | `denied` (terminal) | `ok=false`, `PolicyDenied`, rule and reason |
| `approval_required` | `awaiting_approval` (pending) | `ok=false`, `ApprovalRequired`, the rule that asked |

This closes invariant 2: every new operation receives its first outcome in the
transaction that created it, whatever policy said.

**Decision-to-outcome for an operation whose current outcome is `awaiting_approval`.**
This is the one state where a later invocation can change an operation, so the mapping is
fixed here rather than left to policy authors:

| Approval verdict | Policy decision | Outcome appended | Operation afterwards | `tool_result` |
| :--- | :--- | :--- | :--- | :--- |
| any failed verdict (not consumed) | `deny` written by the runtime, `matched_rule = runtime.approval_rejected`, reason = the verdict; the policy set is not invoked | **`awaiting_approval`** | still pending; a later correct approval can complete it | `ok=false`, `ApprovalRejected`, the verdict |
| `approval_valid` (consumed) | `deny` (some *other* rule refused) | **`denied`** | terminal | `ok=false`, `PolicyDenied`, rule and reason |
| `approval_valid` (consumed) | `approval_required` | fatal configuration error at runtime: transaction rolled back, nothing recorded, operator alerted (see *Failures the journal cannot record*); the operation stays `awaiting_approval` and the artefact stays unconsumed | unchanged | MCP error |
| `approval_valid` (consumed) | `allow` | `applied` or `rejected` from the core | terminal | per the core's result |

A failed presentation never forecloses the operation; only a genuine policy denial or the
core's own verdict does. The `allow` row of the new-operation table is an M2b test, together
with a property test that the null policy returns `allow` for every context; the other
rows of both tables are M3 tests.

## What M2b ships, and what it stubs

The protocol below names admission (tokenization and redaction), policy, and trace
derivation. Those are M2c and M3 deliverables. M2b implements the protocol *shape*
completely and plugs in the simplest conforming component at each seam, so that later
milestones replace an implementation, never the protocol:

- **Admission** is an interface (`Admitter`). M2b ships the identity admitter: identifiers
  are validated by `require_identifier` and passed through, free text is passed through.
  M2c replaces it with the tokenizing, redacting one. Token domain and key version in the
  definition are `none` under the identity admitter, as is the approval verification key,
  and the definition says so.
- **Policy** is an interface. M2b ships the null policy set, version `none`, which returns
  `allow` for every context and still writes a complete `decisions` row, so every
  operation has a decision and the outcome tables above hold from day one. Its row is
  fully specified so it serializes into the v2 `policy_decision` payload without
  sentinels being invented later: `policy_set_version = "none"`, `decision = "allow"`,
  `matched_rule = "none.allow_all"`, `reason = "null policy set: no rules configured"`,
  and a `PolicyContext` with principal `local`, the admitted subject, the command digest,
  the evaluation time from the injected clock, an empty aggregates map, and no approval.
  The artefact wire format and `ledgergate approve` are M3 deliverables, so under M2b's
  identity admitter a `Request` with a non-null `approval` field **fails admission** with
  code `approval_unsupported` (disposition `invalid`). The `approvals` and
  `approval_consumptions` tables exist with their constraints and are tested empty, and
  the test that makes that claim is the one that presents an artefact and asserts the
  `invalid` path. M2c's tokenizing admitter returns the same `approval_unsupported`; from
  M3 the admitter accepts artefacts and the presentation rules below apply.
- **Trace derivation** is M3, with schema v2. M2b exposes the journal for inspection
  (`ledgergate journal dump`) but derives no trace; the M2a replayer is not run against
  a journal in M2b. The roadmap says so.

## Write protocol

One invocation, one SQLite transaction, `BEGIN IMMEDIATE`. SQLite serializes write
transactions; exactly one is active at a time, whatever the process count. Rows are
written in an order that satisfies the immediate foreign keys below: an invocation before
anything that references it, an operation before the invocation that references it.

1. Take the write lock.
2. **Cursor.** If the projection's cursor is not `COALESCE(MAX(outcomes.journal_sequence), 0)`,
   rebuild from `definition` and all outcome rows in order. An `applied` outcome is folded
   by executing the recorded `command` through the core with the recorded effects
   (`entry_id`, `posted_at`) fed back. The core re-validates as it always does; if it
   raises on an `applied` fold the journal is corrupt and this is the integrity failure
   named in step 2. No policy is re-evaluated. An `awaiting_approval`, `denied` or
   `rejected` outcome is folded as a no-op on the books but advances the cursor, so a
   process cannot miss it and the core is never asked to re-raise a recorded rejection. The entry-chain head is checked against the rebuilt projection
   as an integrity test; it is not the cursor, because lifecycle commands leave it
   unchanged.
3. **Admit.** Tokenize every caller identifier ([identifiers-and-redaction](identifiers-and-redaction.md)),
   redact free text, decode the command. On failure (unknown tool, malformed arguments,
   identifier invalid after tokenization): write `invocations` (`invalid`, no operation),
   then the inbound `events` row holding a **failure envelope** rather than the request's
   raw structural form: the tokenized `call_id` if one was recoverable; the tool name only if it is a
   known operator-defined tool; `input_digest` as defined under *Admission input and
   Request* (wire-level correlation is not offered by the journal; that is the transport's
   concern); the structured admission error (a code and the failing field path, no
   values); and the raw payload only after the redactor has run over it as an untyped
   blob, bounded to 4 KiB. Malformed input is exactly where field-aware redaction is
   weakest, so undecodable content is treated as the most sensitive kind, not the least.
   Then `invocation_responses` (`invalid`, no outcome) and the outbound `events` row with
   the error; commit; return. **Scope of the guarantee by milestone:** the envelope's
   *shape* and 4 KiB *bound* apply from M2b, so M2c changes nothing structural; but under
   M2b's identity admitter the redactor is a pass-through and the bounded payload may
   contain unredacted values. Protection of sensitive content begins with M2c, and M2b
   must not be deployed against data that needs it.
4. **Resolve the key** in `operations` and write the invocation:
   - Absent: insert `operations` (`key`, `fingerprint`, canonical `command`), then
     `invocations` (`new`, referencing it). If an approval was presented, an `approvals` row
     with check result `approval_not_applicable` follows the inbound event; the `PolicyContext`
     carries that verdict.
   - Present, fingerprint matches, current outcome terminal: `invocations` (`replay`). If an
     approval was presented, an `approvals` row with check result `approval_not_applicable`
     follows the inbound event.
   - Present, fingerprint matches, current outcome `awaiting_approval`, approval
     presented: `invocations` (`approval`).
   - Present, fingerprint matches, current outcome `awaiting_approval`, no approval:
     `invocations` (`replay`).
   - Present, fingerprint differs: `invocations` (`conflict`, with attempted fingerprint
     and command). If an approval was presented, an `approvals` row with check result
     `approval_not_applicable` follows the inbound event.
5. Write the inbound `events` row (the admitted `tool_call`), referencing the invocation.
6. **Short paths.** For `replay`: `invocation_responses` (`replayed`, naming the
   operation's current outcome row, which is the one the response is rendered from);
   outbound `events` derived from that outcome; commit; return. For `conflict`:
   `invocation_responses` (`conflict`, no outcome); outbound `events` with the conflict
   error; commit; return. Neither writes a decision row: no policy evaluation happened.
   For `approval`:
   validate and consume per *Approval artefacts* (this writes the `approvals` presentation
   row, which references the invocation, hence its position here), then continue.
7. **Decide.** Build the `PolicyContext`, reading aggregates from `outcomes`, `operations`
   (for the amounts a monetary cap needs) and `decisions` in this transaction. Evaluate. Write `decisions`, referencing the
   consumption row if check 4 succeeded. If not `allow`: append outcome per the applicable
   decision-to-outcome table (new operation, or pending operation), **with
   `outcomes.decision` referencing this decision** and `previous_outcome` per *Outcome
   chain*; `invocation_responses` naming that outcome; outbound `events`; commit; return.
8. **Execute.** Run the command through the pure core.
9. Append outcome (`applied` or `rejected`, with effects and heads), **with
   `outcomes.decision` referencing step 7's decision** and `previous_outcome` per
   *Outcome chain*. `invocation_responses` naming that outcome. Outbound `events`. Commit.
10. Render and return the response.

**Crash analysis.** Before commit: nothing exists in the journal, and the process keeps
the projection reference it held before the transaction; the `Ledger` value produced in
step 8 is discarded (it is an immutable value, so nothing has to be undone). A retry runs
afresh. After commit,
before step 10: a complete invocation exists including its outbound event. A retry of a
committed `new` resolves as `replay`. A retry of a committed `approval` resolves as `replay`
if its verdict was `approval_valid` (the operation is now terminal) and as a fresh
`approval` if the verdict failed (the operation is still pending; checks 1 to 3 re-run,
another presentation row is appended, nothing is consumed). A retry of a committed
`conflict` is a fresh `conflict`; a retry of a committed `invalid` is a fresh `invalid`; a retry of a read
is a fresh read. None of these apply anything twice. Every path from step 4 onward that created an operation appends an
outcome in the same transaction (invariant 2).

**Failures the journal cannot record.** `SQLITE_BUSY` past the retry budget, a constraint
violation other than the approval consumption `UNIQUE`, an integrity failure at step 2, a
transport-level I-JSON violation (a number outside the JCS-safe range never reaches
admission), a policy set returning `approval_required` against a consumed approval, or a
non-`LedgerError` exception from the core (a bug): the transaction is rolled back, nothing
is written, the caller receives an MCP error. This is the one class of call with no
journal row, stated rather than hidden: the journal was unavailable, so it could not be the
record.

## Foreign keys

Every reference points to a row written earlier in the same or an earlier transaction;
SQLite foreign keys are enabled and **immediate**, so the write order in the protocols
above is the only legal one and an implementation that reorders it fails at the
constraint, not in review. Targets are `journal_sequence` values.

| Column | References |
| :--- | :--- |
| `outcomes.operation` | `operations` |
| every fact table's `journal_sequence` | `journal.journal_sequence` (the allocator; each fact row's primary key) |
| `outcomes.(previous_outcome, operation)` | `outcomes.(journal_sequence, operation)` (composite; null for the first; needs `UNIQUE (journal_sequence, operation)`) |
| `outcomes.decision` | `decisions` (null when no policy ran) |
| `invocations.operation` | `operations` (null for `read`, `invalid`) |
| `invocation_responses.invocation` | `invocations` (`UNIQUE`) |
| `invocation_responses.outcome` | `outcomes` (null for `conflict`, `invalid`, `read`) |
| `decisions.invocation` | `invocations` |
| `decisions.operation` | `operations` (null for reads) |
| `decisions.presentation` | `approvals` (null when none presented) |
| `decisions.consumption` | `approval_consumptions` (null unless kept) |
| `approvals.invocation` | `invocations` |
| `approval_consumptions.presentation` | `approvals` |
| `approval_consumptions.invocation` | `invocations` |
| `events.invocation` | `invocations` (null only for standalone `message` rows) |
| `reads.invocation` | `invocations` |

Nothing references `decisions` forward from a row written before it. Every outcome
appended in a transaction that wrote a decision references that decision. `outcomes.decision`
is null only for an outcome no policy evaluated, which does not occur: every `new` and
`approval` invocation evaluates policy (the null policy set included), and `replay` and
`conflict` append no outcome. It is nullable only so a migration path is not foreclosed.

## Read protocol

`balance`, `trial_balance`. Reads are not operations and never enter the rebuild. An
*audited* read is recorded, and recording is a write, so it runs under `BEGIN IMMEDIATE`
and accepts serialization; a deferred transaction that upgrades to write after taking its
snapshot can fail with `SQLITE_BUSY` and leave a result matching no recordable state.

1. Lock. 2. Cursor (as write step 2). 3. Admit (as write step 3; an invalid read writes
`invocations` (`invalid`), the failure-envelope inbound event, `invocation_responses`
(`invalid`, no outcome), the outbound event; commits; returns). 4. `invocations` (`read`).
5. Inbound `events`; then, if an approval was presented, an `approvals` row with check result
`approval_not_applicable`. 6. `decisions` if the read is policy-gated. A read is policy-gated iff the configured
policy set declares that read tool gated; the null policy set gates no reads, so M2b's
audited reads write no `decisions` row and their trace carries no `policy_decision`. For a
gated read, `PolicyContext.command_digest` is the invocation's `request_digest`; a read has
no operation and no fingerprint. On `deny`: no `reads`
row; disposition stays `read`; go to 8 with `ok=false`/`PolicyDenied` as the outbound
event and `response = denied` on the response row, so a consumer of `invocation_responses`
alone sees that nothing was served. 7. Serve from the projection; write `reads` with cursor, head and result digest.
8. `invocation_responses` (`read`, or `denied` for a denied gated read; no outcome); outbound `events`; commit; respond.

Unaudited reads of the projection by the process itself are snapshot reads and write
nothing.

## Trace derivation (M3)

`trace(journal) -> Trace` is deterministic and emits schema v2 only. Ordering is
*invocation-anchored*: every event derived from one invocation, from whatever table its
data comes, is placed at `(invocation.journal_sequence, ordinal)` with the fixed ordinal
order in [trace-v2](trace-v2.md), so the `tool_call` precedes the `command_intent` even
though its row was written after the invocation row. `invocation_resolution` names the
exact outcome from `invocation_responses`, never "the operation's current outcome", so a
replay that answered `awaiting_approval` still says so after the operation was later
approved. Standalone message events (rows with a null `invocation`) sit at their own row's sequence. Identifiers, the
definition-derived top level, and whole-journal scope are specified in trace-v2. Not built
in M2b.

## Concurrency

Any number of unaudited readers under WAL. Every journal write, including audited reads,
is a serialized `BEGIN IMMEDIATE` transaction. Multiple writer processes are correct under
write step 2 and not optimized.
