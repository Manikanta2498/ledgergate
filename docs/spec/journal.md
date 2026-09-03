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
| `decisions` | `invocation`, `operation`, canonical serialized `PolicyContext` including the aggregate values read, policy set version, decision, matched rule, reason, `approval`. |
| `approvals` | An approval artefact as presented and as validated: `approval_id`, approver principal, bound `fingerprint`, bound tokenized `key`, bound subject, bound amount and currency, `issued_at`, `expires_at`, signature, validation verdict. |
| `approval_consumptions` | `approval` (`UNIQUE`), the `decision` that consumed it. |
| `events` | Boundary events: the inbound `tool_call` (tool, admitted arguments after redaction, `call_id`) or message, and the outbound `tool_result` data (ok, result or error) the response is rendered from, keyed to their invocation. |
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
`fingerprint`, tokenized `key`, subject, amount and currency. Validation, in this order,
before the approval enters the `PolicyContext`:

1. Signature verifies against the definition's key, else `approval_invalid`.
2. `expires_at` is after the injected evaluation time, else `approval_expired`.
3. Every bound field equals the pending operation's, else `approval_scope_mismatch`.
4. **Reservation.** `SAVEPOINT approval; INSERT INTO approval_consumptions`. A `UNIQUE`
   violation means an earlier *committed* transaction consumed it (writes are serialized,
   so there is no other way): verdict `approval_already_used`. Success means the approval
   is reserved for this transaction.

A failed verdict enters the context as a failed approval; policy sees an approval that is
not usable and decides accordingly (in practice, `deny` with that reason). Nothing is
consumed on a failed verdict. A successful reservation is kept only if the final decision
is `allow`; otherwise `ROLLBACK TO approval` releases it, so a valid approval is never
burned by an unrelated denial. The context therefore always states the true consumption
state, and the `UNIQUE` can never fail after policy has run.

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
   `decisions` in this transaction. Evaluate. Write `decisions`. If not `allow`: release
   any approval reservation; append outcome (`denied` or `awaiting_approval`); outbound
   `events`; commit; return.
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

## Read protocol

`balance`, `trial_balance`. Reads are not operations and never enter the rebuild. An
*audited* read is recorded, and recording is a write, so it runs under `BEGIN IMMEDIATE`
and accepts serialization; a deferred transaction that upgrades to write after taking its
snapshot can fail with `SQLITE_BUSY` and leave a result matching no recordable state.

1. Lock. 2. Cursor (as write step 2). 3. Admit (as write step 3; an invalid read is
recorded with disposition `invalid`). 4. Inbound `events`, `invocations` (`read`).
5. `decisions` if the read is policy-gated; a `deny` skips 6. 6. Serve from the
projection; write `reads` with cursor, head and result digest. 7. Outbound `events`;
commit; respond.

Unaudited reads of the projection by the process itself are snapshot reads and write
nothing.

## Concurrency

Any number of unaudited readers under WAL. Every journal write, including audited reads,
is a serialized `BEGIN IMMEDIATE` transaction. Multiple writer processes are correct under
write step 2 and not optimized.
