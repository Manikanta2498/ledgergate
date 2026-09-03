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

## Global order

Every row in every table carries a `journal_sequence` drawn from one monotonic counter.
It is the projection cursor and the only ordering source. Per-table sequences do not
exist. Trace event order is derived from it (see [trace-v2](trace-v2.md)).

## Tables

All strictly append-only. No row is ever updated or deleted.

| Table | Holds |
| :--- | :--- |
| `definition` | Written once: chart, currency registry, codec version, policy set version, identifier token domain and key version, approval verification key. Free text in the definition (account names) passes the same redactor as everything else. A journal is bound to one definition; changing it means a new journal. |
| `operations` | `key` (tokenized, `UNIQUE`), `fingerprint`, canonical `command`. |
| `outcomes` | `operation`, `previous_outcome`, `outcome` (`applied`, `rejected`, `denied`, `awaiting_approval`), error type and message, `entry_id`/`posted_at` when appended, `head_before`, `head_after`, `ledger_sequence`, `decision`. |
| `invocations` | `operation` (null for reads and invalid calls), `requested_at`, `principal`, `disposition` (`new`, `replay`, `conflict`, `approval`, `read`, `invalid`), `attempted_fingerprint`, `attempted_command` (what *this* attempt asked, so a conflict shows both sides), `call_id`. |
| `decisions` | `invocation`, `operation`, canonical serialized `PolicyContext` including the aggregate values read, policy set version, decision, matched rule, reason, the approval presentation row considered, and the `consumption` row if one was kept. |
| `approvals` | One row per *presentation* of an artefact (a presentation row's identity is its `journal_sequence`): the logical `approval_id` from the artefact, approver principal, bound `fingerprint`, bound tokenized `key`, bound subject, bound amount and currency, `issued_at`, `expires_at`, signature, and this presentation's validation verdict. Presenting the same artefact twice appends two rows with two verdicts. |
| `approval_consumptions` | logical `approval_id` (`UNIQUE`), the presentation row, the consuming `invocation`. The `UNIQUE` is on the logical id, so an artefact is consumable once however many times it is presented. The decision that used it references this row, not the reverse, so the row can exist before the decision does. |
| `events` | Boundary events: the inbound `tool_call` (tool, admitted arguments after redaction, `call_id`) or message, and the outbound `tool_result` data the response is rendered from (`ok=true` with result, or `ok=false` with error type and message; a policy denial is `ok=false`, type `PolicyDenied`, message the rule and reason), keyed to their invocation. |
| `reads` | For read tools: the `journal_sequence` and head the projection was at when served, and the result digest. |

## Invariants

1. Every row a response depends on is committed before the response is rendered.
2. Every operation has at least one outcome in the same transaction that created it.
3. An approval is consumed at most once, enforced by `approval_consumptions.approval UNIQUE`,
   and only by a decision of `allow`.
4. A `tool_call` row never exists without the data for its `tool_result`.
5. The projection cursor equals the journal's max `journal_sequence` whenever a command is
   evaluated against it.

## Approval artefacts

Issued out of band (in M4, by the operator via `ledgergate approve`), signed with a key
whose verification counterpart is in `definition`. Binds to exactly one pending operation:
`fingerprint`, tokenized `key`, subject, amount and currency.

**Validation and reservation**, performed inside the write transaction *before* the
`PolicyContext` is built. First the `approvals` presentation row is written (outside any
savepoint, so the audit of the attempt survives whatever follows). Then the checks run
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

**Settling the reservation** happens in step 6 of the write protocol, *before* the
decision row is written, because `ROLLBACK TO` undoes everything after the savepoint and
the decision must survive:

- Final decision `allow`: `RELEASE reserve`. Then write `decisions`, which references the
  consumption row. The consumption row itself references the presentation and the
  invocation, not the decision, precisely so it can be written before the decision exists.
  The `UNIQUE` on `approval_id` has held since step 4, so nothing consumed it in between.
- Final decision not `allow`: `ROLLBACK TO reserve; RELEASE reserve` (removes only the
  reservation; the presentation row and everything before the savepoint remain). Then
  write `decisions` with the verdict, then append the outcome. The approval remains
  consumable by a later, correct attempt.

The context therefore always states the true consumption state, and the `UNIQUE` cannot
fail after policy has run because the reservation was taken before it.

## Write protocol

One invocation, one SQLite transaction, `BEGIN IMMEDIATE`. SQLite serializes write
transactions; exactly one is active at a time, whatever the process count.

1. Take the write lock.
2. **Cursor.** If the projection's cursor is not the journal's max `journal_sequence`,
   rebuild from `definition` and all outcome rows in order. The entry-chain head is checked
   against the rebuilt projection as an integrity test; it is not the cursor, because
   lifecycle commands leave it unchanged.
3. **Admit.** Tokenize every caller identifier ([identifiers-and-redaction](identifiers-and-redaction.md)),
   redact free text, decode the command. On failure (unknown tool, malformed arguments,
   identifier invalid after tokenization): write inbound `events`, `invocations`
   (`invalid`), outbound `events` with the error; commit; return. No operation exists.
4. Write the inbound `events` row.
5. **Resolve the key** in `operations`:
   - Absent: insert `operations` (`key`, `fingerprint`, canonical `command`), then
     `invocations` (`new`). Continue to 6.
   - Present, fingerprint matches, current outcome terminal: `invocations` (`replay`);
     outbound `events` from the stored outcome; commit; return. No decision row.
   - Present, fingerprint matches, current outcome `awaiting_approval`, approval presented:
     validate and reserve per *Approval artefacts*; `invocations` (`approval`); continue to
     6 with the verdict in the context.
   - Present, fingerprint matches, current outcome `awaiting_approval`, no approval:
     `replay` of that outcome.
   - Present, fingerprint differs: `invocations` (`conflict`, with attempted fingerprint
     and command); outbound `events`; commit; return. No decision row.
6. **Decide.** Build the `PolicyContext`, reading aggregates from `outcomes` and
   `decisions` in this transaction. Evaluate. **Settle any approval reservation first**
   (release on `allow`, roll back the savepoint otherwise; see *Approval artefacts*), then
   write `decisions`. If not `allow`: append outcome (`denied` or `awaiting_approval`);
   outbound `events`; commit; return.
7. **Execute.** Run the command through the pure core.
8. Append outcome (`applied` or `rejected`, with effects and heads). Outbound `events`.
   Commit.
9. Render and return the response.

**Crash analysis.** Before commit: nothing exists; a retry runs afresh. After commit,
before 9: a complete invocation exists including its outbound event; a retry resolves as
`replay`. Every path from step 5 onward that created an operation appends an outcome in
the same transaction (invariant 2).

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

1. Lock. 2. Cursor (as write step 2). 3. Admit (as write step 3; an invalid read is
recorded with disposition `invalid`). 4. Inbound `events`, `invocations` (`read`).
5. `decisions` if the read is policy-gated. On `deny`: no `reads` row is written, the
outbound event is `ok=false`/`PolicyDenied`, the disposition stays `read`; go to 7.
6. Serve from the projection; write `reads` with cursor, head and result digest.
7. Outbound `events`; commit; respond.

Unaudited reads of the projection by the process itself are snapshot reads and write
nothing.

## Concurrency

Any number of unaudited readers under WAL. Every journal write, including audited reads,
is a serialized `BEGIN IMMEDIATE` transaction. Multiple writer processes are correct under
write step 2 and not optimized.
