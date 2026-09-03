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

## Sequences

Three distinct things, kept distinct because conflating them was a review finding:

- **`journal_sequence`**: every row in every table carries one, from a single monotonic
  counter. It orders every durable fact. Per-table sequences do not exist.
- **Projection cursor**: the `journal_sequence` of the latest **outcome** row folded into
  the projection. Only outcomes change the ledger; invocations, events, decisions,
  approvals and reads advance `journal_sequence` without touching it. Freshness is
  therefore "is my cursor equal to `MAX(outcomes.journal_sequence)`", not to the global
  maximum, which is always ahead of any projection.
- **Trace order**: derived, not stored; defined in [trace-v2](trace-v2.md). It anchors
  every event of an invocation to that invocation's `journal_sequence`, because the
  foreign keys force the invocation row to be written *before* the inbound event that
  conceptually precedes it.

## Tables

All strictly append-only. No row is ever updated or deleted.

| Table | Holds |
| :--- | :--- |
| `definition` | Written once: chart, currency registry, codec version, policy set version, identifier token domain and key version, approval verification key. Free text in the definition (account names) passes the same redactor as everything else. A journal is bound to one definition; changing it means a new journal. |
| `operations` | `key` (tokenized, `UNIQUE`), `fingerprint`, canonical `command`. |
| `outcomes` | `operation`, `previous_outcome`, `outcome` (`applied`, `rejected`, `denied`, `awaiting_approval`), error type and message, `entry_id`/`posted_at` when appended, `head_before`, `head_after`, `ledger_sequence`, `decision`. |
| `invocations` | `operation` (null for reads and invalid calls), `requested_at`, `principal`, `disposition` (`new`, `replay`, `conflict`, `approval`, `read`, `invalid`), `attempted_fingerprint`, `attempted_command` (what *this* attempt asked, so a conflict shows both sides), `call_id`. |
| `decisions` | `invocation`, `operation`, canonical serialized `PolicyContext` including the aggregate values read, policy set version, decision, matched rule, reason, the approval presentation row considered, and the `consumption` row if one was kept. |
| `approvals` | One row per *presentation* of an artefact (a presentation row's identity is its `journal_sequence`), referencing the presenting `invocation`: the logical `approval_id` from the artefact, approver principal, bound `fingerprint`, bound tokenized `key`, bound subject, bound amount and currency, `issued_at`, `expires_at`, signature, and this presentation's validation verdict. Presenting the same artefact twice appends two rows with two verdicts. |
| `approval_consumptions` | logical `approval_id` (`UNIQUE`), the presentation row, the consuming `invocation`. The `UNIQUE` is on the logical id, so an artefact is consumable once however many times it is presented. The decision that used it references this row, not the reverse, so the row can exist before the decision does. |
| `events` | Boundary events: the inbound `tool_call` (tool, admitted arguments after redaction, `call_id`) or message, and the outbound `tool_result` data the response is rendered from (`ok=true` with result, or `ok=false` with error type and message; a policy denial is `ok=false`, type `PolicyDenied`, message the rule and reason), keyed to their invocation. |
| `reads` | For read tools: the `journal_sequence` and head the projection was at when served, and the result digest. |

## Invariants

1. Every row a response depends on is committed before the response is rendered.
2. Every operation has at least one outcome in the same transaction that created it.
3. An approval is consumed at most once, enforced by `approval_consumptions.approval UNIQUE`,
   and only by a decision of `allow`.
4. A `tool_call` row never exists without the data for its `tool_result`.
5. Whenever a command is evaluated against the projection, the projection's cursor
   equals `MAX(outcomes.journal_sequence)` as of the start of the transaction. Rows this
   transaction writes before the core runs (invocation, events, decision) are not
   outcomes and do not move it; the outcome this transaction appends becomes the new
   cursor on commit.
6. Every row is written after every row it references (immediate foreign keys); the
   protocols' step order is the only legal one.

## Approval artefacts

Issued out of band (in M4, by the operator via `ledgergate approve`), signed with a key
whose verification counterpart is in `definition`. Binds to exactly one pending operation:
`fingerprint`, tokenized `key`, subject, amount and currency.

**Validation and reservation**, performed inside the write transaction *before* the
`PolicyContext` is built and after the invocation row exists (the presentation references
it). First the `approvals` presentation row is written (outside any savepoint, so the
audit of the attempt survives whatever follows). Then the checks run
**in order and short-circuit: the first failure is the verdict, and no later check runs**.
In particular, the reservation in check 4 is attempted if and only if checks 1 to 3 all
passed; an invalid, expired or mis-scoped artefact never touches `approval_consumptions`.

1. Signature verifies against the definition's key, else verdict `approval_invalid`.
2. `expires_at` is after the injected evaluation time, else `approval_expired`.
3. Every bound field equals the pending operation's, else `approval_scope_mismatch`.
4. **Reservation.** `SAVEPOINT reserve; INSERT INTO approval_consumptions (approval_id,
   presentation, invocation)`. A `UNIQUE` violation means an earlier *committed*
   transaction consumed this logical approval (writes are serialized, so there is no other
   way): `ROLLBACK TO reserve; RELEASE reserve`, verdict `approval_already_used`. Success:
   verdict `approval_valid`, the approval is reserved for this transaction, and the
   savepoint stays open.

The verdict enters the `PolicyContext`. Nothing is consumed on any verdict other than
`approval_valid`.

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
| any failed verdict | (policy sees an unusable approval and returns `deny` with the verdict as reason) | **`awaiting_approval`** | still pending; a later correct approval can complete it | `ok=false`, type `ApprovalRejected`, message the verdict |
| `approval_valid` | `deny` (some *other* rule refused) | **`denied`** | terminal | `ok=false`, `PolicyDenied`, rule and reason |
| `approval_valid` | `approval_required` | not reachable: a valid approval satisfies the rule that asked for it; a policy set that asks again is a configuration error and is rejected at definition load | | |
| `approval_valid` | `allow` | `applied` or `rejected` from the core | terminal | per the core's result |

A failed presentation therefore never forecloses the operation; only a genuine policy
denial or the core's own verdict does. Each row of this table is a required test.

**Settling the reservation** happens in step 7 of the write protocol, *before* the
decision row is written, because `ROLLBACK TO` undoes everything after the savepoint and
the decision must survive:

- Final decision `allow`: `RELEASE reserve`. Then write `decisions`, which references the
  consumption row. The consumption row itself references the presentation and the
  invocation, not the decision, precisely so it can be written before the decision exists.
  The `UNIQUE` on `approval_id` has held since check 4, so nothing consumed it in between.
- Final decision not `allow`: `ROLLBACK TO reserve; RELEASE reserve` (removes only the
  reservation; the presentation row and everything before the savepoint remain). Then
  write `decisions` with the verdict, then append the outcome. The approval remains
  consumable by a later, correct attempt.

The context therefore always states the true consumption state, and the `UNIQUE` cannot
fail after policy has run because the reservation was taken before it.

## What M2b ships, and what it stubs

The protocol below names admission (tokenization and redaction), policy, and trace
derivation. Those are M2c and M3 deliverables. M2b implements the protocol *shape*
completely and plugs in the simplest conforming component at each seam, so that later
milestones replace an implementation, never the protocol:

- **Admission** is an interface (`Admitter`). M2b ships the identity admitter: identifiers
  are validated by `require_identifier` and passed through, free text is passed through.
  M2c replaces it with the tokenizing, redacting one. Token domain and key version in the
  definition are `none` under the identity admitter and the definition says so.
- **Policy** is an interface. M2b ships the null policy set, version `none`, which returns
  `allow` for every context and still writes a complete `decisions` row, so every
  operation has a decision and the outcome tables above hold from day one. Its row is
  fully specified so it serializes into the v2 `policy_decision` payload without
  sentinels being invented later: `policy_set_version = "none"`, `decision = "allow"`,
  `matched_rule = "none.allow_all"`, `reason = "null policy set: no rules configured"`,
  and a `PolicyContext` with principal `local`, the admitted subject, the command digest,
  the evaluation time from the injected clock, an empty aggregates map, and no approval.
  Approval artefacts are not presented under the null policy (nothing asks for one); the
  `approvals` tables exist and are tested empty.
- **Trace derivation** is M3, with schema v2. M2b exposes the journal for inspection
  (`ledgergate journal dump`) but derives no trace; the M2a replayer is not run against
  a journal in M2b. The roadmap says so.

## Write protocol

One invocation, one SQLite transaction, `BEGIN IMMEDIATE`. SQLite serializes write
transactions; exactly one is active at a time, whatever the process count. Rows are
written in an order that satisfies the immediate foreign keys below: an invocation before
anything that references it, an operation before the invocation that references it.

1. Take the write lock.
2. **Cursor.** If the projection's cursor is not `MAX(outcomes.journal_sequence)`,
   rebuild from `definition` and all outcome rows in order. An `awaiting_approval` or
   `denied` outcome is folded as a no-op on the books but advances the cursor, so a
   process cannot miss it. The entry-chain head is checked against the rebuilt projection
   as an integrity test; it is not the cursor, because lifecycle commands leave it
   unchanged.
3. **Admit.** Tokenize every caller identifier ([identifiers-and-redaction](identifiers-and-redaction.md)),
   redact free text, decode the command. On failure (unknown tool, malformed arguments,
   identifier invalid after tokenization): write `invocations` (`invalid`, no operation,
   with whatever `call_id` and attempted data admission recovered), then the inbound
   `events` row as received, then the outbound `events` row with the error; commit;
   return. No operation exists.
4. **Resolve the key** in `operations` and write the invocation:
   - Absent: insert `operations` (`key`, `fingerprint`, canonical `command`), then
     `invocations` (`new`, referencing it).
   - Present, fingerprint matches, current outcome terminal: `invocations` (`replay`).
   - Present, fingerprint matches, current outcome `awaiting_approval`, approval
     presented: `invocations` (`approval`).
   - Present, fingerprint matches, current outcome `awaiting_approval`, no approval:
     `invocations` (`replay`).
   - Present, fingerprint differs: `invocations` (`conflict`, with attempted fingerprint
     and command).
5. Write the inbound `events` row (the admitted `tool_call`), referencing the invocation.
6. **Short paths.** For `replay`: outbound `events` derived from the current outcome;
   commit; return. For `conflict`: outbound `events` with the conflict error; commit;
   return. Neither writes a decision row: no policy evaluation happened. For `approval`:
   validate and reserve per *Approval artefacts* (this writes the `approvals` presentation
   row, which references the invocation, hence its position here), then continue.
7. **Decide.** Build the `PolicyContext`, reading aggregates from `outcomes` and
   `decisions` in this transaction. Evaluate. Settle any approval reservation first
   (release on `allow`, roll back the savepoint otherwise; see *Approval artefacts*), then
   write `decisions`. If not `allow`: append outcome per the applicable
   decision-to-outcome table (new operation, or pending operation); outbound `events`;
   commit; return.
8. **Execute.** Run the command through the pure core.
9. Append outcome (`applied` or `rejected`, with effects and heads). Outbound `events`.
   Commit.
10. Render and return the response.

**Crash analysis.** Before commit: nothing exists; a retry runs afresh. After commit,
before step 10: a complete invocation exists including its outbound event; a retry
resolves as `replay`. Every path from step 4 onward that created an operation appends an
outcome in the same transaction (invariant 2).

**Failures the journal cannot record.** `SQLITE_BUSY` past the retry budget, a constraint
violation other than the approval reservation, an integrity failure at step 2, or a
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
| `outcomes.previous_outcome` | `outcomes` (null for the first) |
| `outcomes.decision` | `decisions` (null when no policy ran) |
| `invocations.operation` | `operations` (null for `read`, `invalid`) |
| `decisions.invocation` | `invocations` |
| `decisions.operation` | `operations` (null for reads) |
| `decisions.presentation` | `approvals` (null when none presented) |
| `decisions.consumption` | `approval_consumptions` (null unless kept) |
| `approvals.invocation` | `invocations` |
| `approval_consumptions.presentation` | `approvals` |
| `approval_consumptions.invocation` | `invocations` |
| `events.invocation` | `invocations` |
| `reads.invocation` | `invocations` |

Nothing references `decisions` forward from a row written before it; that is the property
the reservation ordering depends on.

## Read protocol

`balance`, `trial_balance`. Reads are not operations and never enter the rebuild. An
*audited* read is recorded, and recording is a write, so it runs under `BEGIN IMMEDIATE`
and accepts serialization; a deferred transaction that upgrades to write after taking its
snapshot can fail with `SQLITE_BUSY` and leave a result matching no recordable state.

1. Lock. 2. Cursor (as write step 2). 3. Admit (as write step 3; an invalid read writes
`invocations` (`invalid`) then its events, commits, returns). 4. `invocations` (`read`).
5. Inbound `events`. 6. `decisions` if the read is policy-gated. On `deny`: no `reads`
row, outbound event `ok=false`/`PolicyDenied`, disposition stays `read`; go to 8.
7. Serve from the projection; write `reads` with cursor, head and result digest.
8. Outbound `events`; commit; respond.

Unaudited reads of the projection by the process itself are snapshot reads and write
nothing.

## Trace derivation (M3)

`trace(journal) -> Trace` is deterministic and emits schema v2 only. Ordering is
*invocation-anchored*: every event derived from one invocation, from whatever table its
data comes, is placed at `(invocation.journal_sequence, ordinal)` with the fixed ordinal
order in [trace-v2](trace-v2.md), so the `tool_call` precedes the `command_intent` even
though its row was written after the invocation row. Standalone message events sit at
their own row's sequence. Identifiers, the definition-derived top level, and
whole-journal scope are specified in trace-v2. Not built in M2b.

## Concurrency

Any number of unaudited readers under WAL. Every journal write, including audited reads,
is a serialized `BEGIN IMMEDIATE` transaction. Multiple writer processes are correct under
write step 2 and not optimized.
