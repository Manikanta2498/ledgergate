<!--
SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
SPDX-License-Identifier: Apache-2.0
-->

# Authenticated principals and named approvers (M8a)

ADR-0002 §3: "a mandate without an authenticated principal is not a mandate; the server
refuses a network transport until then." Today every invocation is attributed to one
transport-trusted principal (`--principal`, default `local`), and an approval artefact's
`approver` is a label the signer wrote, verified against one journal-wide key: the label says
who *claims* to have approved. M8a makes both facts authenticated without a network listener:
a **principal registry** with **signed requests**, so an invocation's principal is the holder
of a registered key; and an **approver registry**, so an artefact's approver is the holder of
the key registered under that name, and a policy line can require a named approver. The
network transport (M8b) and external execution with an outbox, together with the cross-clone
consumption authority (M8c), are split out and not claimed here. M8a is what both need first,
and it is testable end to end over stdio.

## Schema 7, not a migration

M8a is journal **schema 7**. As for every earlier bump (`journal.md`, *Binding*; `trace-v2.md`,
*Segmentation*), `open` and `derive` refuse a schema-6 journal; a journal is re-created, never
migrated (no row is ever updated). There is therefore no "legacy" mode inside a journal: every
schema-7 journal has both registries, seeded at `create`, and the rules below hold from its
first row. The corpus is regenerated under schema 7 and its cassettes re-pinned.

## Principals

A **principal** is an identifier (the existing grammar) with an authentication kind:

| Kind | Who it is | How an invocation is attributed to it |
| :-- | :-- | :-- |
| `transport` | the operator of the process | as today: `serve --principal NAME`; the process owner is trusted because they own the process |
| `signed` | the holder of an Ed25519 key registered under the name | the request carries an `auth` envelope with a signature over its canonical content, verified before the command is decoded |

### The registry is a log

`principal_events` is append-only, like every journal table: `journal_sequence`, `principal`,
`action` (`add` | `revoke`), `kind` (on `add`), `verification_key` (base64url Ed25519 on a
`signed` add, null otherwise), `by` (the CLI's transport principal that appended it; a registry event has no invocation), `at` (the
transaction's single clock reading). Monotonicity per name is enforced by a partial
`UNIQUE (principal) WHERE action = 'add'` and a `BEFORE INSERT` trigger (a `CHECK` cannot see
other rows): an `add` requires no prior row for the name, a `revoke` requires a prior `add`
and no prior `revoke`; so exactly one `add`, at most one `revoke`, and never an `add` after a
`revoke` (a new key
is a new name, so "who was `treasury-agent` at sequence *n*" has one answer, computed by
walking the log to *n*). A name is **live at *n*** iff its `add` is at or before *n* and no
`revoke` is. `approver_events` is the same table shape for approvers (`kind` absent).

Registries are **not** part of the definition: adding or revoking a name changes no digest
and is not a binding check at `open`; it is history, like an invocation. The one link to the
definition is the bootstrap: `create` writes the definition and, in the same transaction, the
first event, `add` of the transport principal named by `--principal` (default `local`), with
`by` equal to itself, the single stated exemption from "`by` is live at its sequence": the act
of creating the journal is the operator's own attribution. `create --approver NAME=KEYFILE`
(repeatable) appends approver adds in the *same create transaction* (a crash leaves either a
complete journal with its seeds or no journal), and the existing `create`-time rule "a
policy that can require approval needs someone who can approve" now reads: `approve_above`
non-empty requires at least one approver seeded, and every name in any line's `approvers`
(below) must be seeded. Later commands append: `ledgergate journal principal add PATH NAME
--verification-key-file FILE` (`signed`; a `transport` add takes no key), `... revoke PATH
NAME`, and `ledgergate journal approver add|revoke ...`. **Registry mutation is the process
owner's alone**: these are CLI commands run by a live `transport` principal (`--principal`,
default `local`, refused if not live), never a tool `serve` exposes and never something a
`signed` principal can do, so an agent cannot register itself as an approver or a colleague as
a principal. A registry event is a *standalone row in its own transaction*, allocator row
included, with no `invocations` row (like a `message`, which is why it costs 1 in the
capacity formula), and its trace event has no invocation anchor; `by` is the CLI's transport
principal, live at that sequence.

### Attribution of every invocation

Every invocation row carries `principal` and `authentication`, and every `open`, every
invocation transaction and every registry transaction holds the registry rule (a standalone
`message` row is unattributed, as today):

- `serve --principal NAME` (and every CLI command that writes) refuses to start unless `NAME`
  is a live `transport` principal (exit `2`); if `NAME` is revoked *during* a session (another
  process appended the revoke), each later call is recorded `invalid: revoked_principal` under
  the write lock, never a silent success and never an unrecorded refusal.
- A request **without** an `auth` member is a transport invocation: `principal` = the session's,
  `authentication` = `transport`.
- A request **with** an `auth` member is a signed invocation: attributed by the signature, not
  the session (a signed request over any stdio session is accepted; the session's principal
  is not consulted for *attribution*, though its liveness is checked first, below). `principal` = the envelope's, `authentication` = `signed`, once verified.
- A request whose `auth` member fails one of the **clockless** checks (shape, unknown
  principal, bad signature) is recorded `invalid` with the cause below; `principal` = **the
  session's transport principal** (who delivered it; the claimed name is caller text and is
  never stored *as `principal`*, per `identifiers-and-redaction.md` §4: it resolves to a
  registry name or it is content, and content lives only inside the redacted failure-envelope
  blob), `authentication` = `rejected`.
- Attribution is **fixed at signature verification**: a verified envelope whose request then
  fails (`request_expired`, `replayed_call`, or a command that does not decode) is an
  `invalid` row with `principal` = the envelope's and `authentication` = `signed`, since the
  signer is known and pretending otherwise would be a then-versus-now error. The **replay set**
  is the `(principal, call_id)` pairs of verified signed invocations whose disposition is not
  `invalid`: a rejected envelope never enters it, and neither does an expired or undecodable
  one, so nothing a third party can send blocks a principal's own later call id.
- Order of checks on one request: the session's `revoked_principal` check first (a revoked
  operator's session delivers nothing, envelope or not), then the envelope's clockless checks,
  then admission of the command, then, at the single reading, expiry and replay.

`PolicyContext.principal` is the authenticated principal, so a policy line can name it.

### The `auth` envelope

The admission input (`journal.md`, *Admission input and Request*) gains one optional member,
`auth`; `serve` forwards exactly one `_meta` member, `params._meta.ledgergate`, as it
(`mcp-runtime.md`, step 4, amended: `_meta` is otherwise still not forwarded). Its shape:

```json
{"principal": "treasury-agent", "expires_at": "2026-09-06T12:00:30+00:00", "signature": "<base64url Ed25519>"}
```

The signature is over `canonical_bytes` (JCS) of a document *constructed* from the request:
the step-4 value's `tool`, `call_id`, `arguments`, `key` and `approval` (an absent member is
signed as `null`, since step 4 omits absent members and the signer and the journal must agree
on one form), plus `journal_id` and the envelope's `principal` and `expires_at` (RFC 3339 with
an offset, normalised as artefact timestamps are), the signature itself excluded:

```json
{"journal_id": "<the journal's>", "call_id": "<the call's, as serve derives it>",
 "tool": "...", "arguments": {...}, "key": "..." | null, "approval": {...} | null,
 "principal": "treasury-agent", "expires_at": "..."}
```

so a signature cannot be moved between journals, calls, tools, arguments, keys or artefacts,
and `expires_at` is covered. A client learns `journal_id` from `initialize`'s
`result._meta.ledgergate.journal_id` (added; `_meta` is where MCP puts such things) or from
`ledgergate journal id PATH`.

**Where each check runs.** Admission is clockless and reads nothing outside its scope; the
journal hands it, under the lock at step 3, the live `signed` principals and the `journal_id` as part of the
`AdmissionScope` (as it already hands the currency registry), so it does what needs no clock:
`auth` present but not this shape → `invalid: authentication_malformed`; `principal` not a
live `signed` principal at the current head → `invalid: unknown_principal` (a `transport`
name or a revoked name is unknown *as a signer*); signature does not verify over the
recomputed bytes → `invalid: bad_signature`. Admission runs on the pre-tokenization value
(`admission.py` already sees the raw request), which is the value the client signed. The two
checks that need the clock run at the protocol's **single reading**: in the write protocol at
step 4 and in the read protocol at its own reading (`journal.md`), `expires_at <=
requested_at` → `invalid: request_expired`, and `(principal, call_id)` in the replay set →
`invalid: replayed_call` (enforced by a partial `UNIQUE` index on `invocations (principal, call_id) WHERE
authentication = 'signed' AND disposition <> 'invalid'`, so the `SELECT` that produces the
recorded refusal is, as for check 4, merely the optimisation and the constraint is the
guarantee); such a row is written in the step-3 failure-envelope shape (an
`invalid` invocation with a null `request_digest`, the redacted raw payload including `auth`
as an untyped blob, and its keyed `input_digest`), the one shape `invalid` has (a replayed message is refused; a legitimate retry is a *new* call with the *same idempotency key*, which the write protocol answers as it always has). The one-reading rule is untouched.

**What is stored.** Every invocation whose `authentication` is `signed` (a verified envelope,
whatever the disposition, `request_expired` and `replayed_call` rows included) persists
`auth_principal`, `auth_expires_at` and `auth_signature` on its row (an intra-row `CHECK`, as the `approvals` table already has for verified-only fields: the three are non-null iff `authentication = 'signed'`, `principal = auth_principal` when signed, and `authentication = 'rejected'` implies `disposition = 'invalid'`), like a presentation persists an artefact; the signature is
evidence of *who*, not something a later verifier can recompute (arguments and call ids are
tokenized before storage, so the signed bytes are gone by design), and the trace carries
`authentication` and `principal`, not the signature.

### Keys

Ed25519, as approvals already are. `ledgergate keygen --seed-file FILE` writes a seed (mode
0600) and prints the verification key; a seed never enters a journal. Compromise is handled by
`revoke` and a new name (a principal cannot revoke itself: the `BEFORE INSERT` trigger requires `by` to have a `transport` `add`
and no `revoke` in `principal_events` (a subselect, for `approver_events` too) and, on a
`revoke`, `by <> name`, the bootstrap row exempt
because the table is then empty; so a journal whose only transport principal is `local` adds
another before `local` can go; stated, since operators will try it); rotation under one name is not offered (two keys over time under one
name makes "who signed this" a question about the clock, and the clock is the signer's).
`ledgergate sign --seed-file FILE --journal-id ID --expires-in SECONDS REQUEST.json` produces
the envelope for a request value (`expires_at` = the signer's clock plus `--expires-in`, at most 86,400 seconds: the journal refuses an envelope whose `expires_at` is more than a day past `requested_at` as `authentication_malformed`, since the replay set already bounds reuse and a request valid for years is a signed blank cheque; in the
corpus, a `sign_as` step takes `expires_in_seconds` against the runner's peeked clock, as a
`sign` step does, so the behavioural digest is stable), what a client library would do, so tests and the corpus can produce signed
requests without one.

## Approvers

Approval check 1 (`journal.md`, *Approval protocol*) becomes: the artefact's `approver` names
an approver **live at the presenting invocation's sequence** *and* the signature verifies
under that entry's key; otherwise `approval_invalid`. The approver label is therefore
authenticated. `ledgergate approve --seed-file FILE --approver NAME` refuses to write a label
whose registered key is not the seed's. An artefact issued before its approver's `add` and
presented after is valid (liveness is judged at presentation, the "now" of the check); one
presented after a `revoke` is `approval_invalid` (stated: then versus now, the artefact's
validity is a fact about the presentation).

### A named approver is a runtime check, informed by the policy

`ThresholdPolicySet.approve_above` lines gain an optional `approvers: [names]`; the set's
`configuration()` omits the field when absent, so every existing configuration digest is
unchanged, and includes it when present, so a changed list is a changed policy; an empty list
is refused at construction (a line nobody may approve is stranded by construction and would
pass the seeding rule vacuously). The policy
protocol gains one **pure** method, `approvers_for(command_kind, currency, amount) ->
frozenset[str] | None`: the names admitted by the first `approve_above` line matching those
three fields by `evaluate`'s own predicate (kind and currency equal, amount above the line;
any `None` input, a command without an amount, yields `None`), or `None` when no line matches or the matching line has no `approvers` (the
null set always returns `None`). It is deliberately *not* a prediction of `evaluate`: it
reads nothing but the `approve_above` lines, needs no context, no subject, no aggregates, so
it runs before any `PolicyContext` exists and the failed-verdict rule ("on a failed verdict
nothing of the set ran") keeps its meaning, since only this one line-lookup ran and the
persisted context says so through the three fields it already carries. The journal calls it,
guarded like every policy call (an exception is a configuration fault), as **check 1b**,
immediately after check 1 and before check 2 (so an artefact that is expired or mis-scoped
*and* wrongly approved reports the wrong approver, and a verifier's rule below is exact), with
the three fields of the *presenting request's* command, the ones the persisted context will
carry:
a verified artefact whose authenticated approver is not in the set is the failed verdict
`approval_wrong_approver`, decided by the runtime as the other failed verdicts are
(`runtime.approval_rejected`, reason = the verdict; nothing is consumed; the operation stays
pending). The closed verdict vocabulary grows by that one value in `approvals.check_result`,
`decisions.approval_verdict` and v2's `Verdict`, part of the schema-7 change. The existing
rule that a policy asking for approval *after* a valid artefact was consumed is a
configuration error stands: the wrong-approver case never reaches consumption, so it cannot
trip it. Check 1 is deterministic under the write lock rather than pure in the old sense: it
reads the registry at the presenting sequence.

A line naming an approver later revoked is not a fault (the registry is history, the
definition is not), but its operations are stranded: every name a line may ever accept was
seeded at `create`, a revoked name can never be re-added (a new key is a new name), and the
line itself is in the definition, so once the last admitted approver is revoked nothing in
this journal can complete those operations. The remedy is the one every definition change
has (`journal.md`, *Tables*, `definition`: "changing it means a new journal"): a new journal
under a new definition, the pending operations left behind with the old one. `journal
pending` lists each pending operation's admitted approvers and which are live, so an operator
sees the stranding before, not after, the last revoke.

## Trace v2 (additive)

- `invocation_resolution` gains `principal` (Identifier) and `authentication`
  (`transport` | `signed` | `rejected`), derived from the invocation row, present in every
  schema-7 derivation and absent in lifted or pre-M8a documents (optional fields).
- `policy_decision.context.approval` gains `approver` (the authenticated name when check 1
  passed, else `null`), so a verifier can recompute `approvers_for(command_kind, currency, amount)` from the three
  fields every context carries (a failed-verdict context too) and check that
  `approval_wrong_approver` was the right verdict and that `approval_valid` was admitted by
  the line; `decision_recomputes` does exactly that for `ThresholdPolicySet` contexts (a runtime
  rule is still never recomputed as a *policy* decision; this is recomputing the input it
  keyed on).
- Two event types, `principal_change` and `approver_change` (`name`, `action`, `kind`, `by`,
  `at`), at their `journal_sequence` position, so a verifier computes liveness at any sequence.
- One invariant, `attributions_are_registered`: every resolution whose `authentication` is
  not `rejected` names a principal live at its sequence (a `transport` one as a `transport`
  add, a `signed` one as a `signed` add), except an `invalid: revoked_principal` row, whose
  principal must have a `revoke` before it; a `rejected` row names the session's transport
  principal, live; every non-null `context.approval.approver` (check 1 passed; a verified
  `approval_not_applicable` presentation has none, since check 1 did not run, and its `verified`
  flag is computed against the registry at the presenting sequence as check 1 would) is live
  at its sequence; every change event's `by` is a live *transport* principal at its sequence, except the first
  event of the trace when it is the bootstrap `add` of a transport principal by itself.
  `no_evidence` for a document without change events (a lifted v1, an earlier v2).
- The v2 capacity bound (`journal.md`, *Segmentation*; `mcp-runtime.md`) counts registry
  events alongside invocations and null-invocation events; the formula is amended.

`invalid` causes gain `authentication_malformed`, `unknown_principal`, `bad_signature`,
`request_expired`, `replayed_call`, `revoked_principal`. **Where the cause is carried.** Today
an `invalid` call's `tool_result.error.type` is the fixed string `AdmissionError` and the
admission code (`unknown_tool`, `missing_key`, ...) lives only in the journal's inbound
failure envelope, which no trace carries; a trace therefore cannot say *which* refusal
contained a call. Schema 7 changes the `invalid` outbound body: `error.type` **is the
admission cause code** (the closed vocabulary `admission.py` already defines, plus the six
above), `error.message` the path alone (`$` for the whole value), the code having moved to the type. This is a body-shape change the schema-7 bump
licenses (`journal.md`, *Tables*, `events`), the derived `tool_result.error.type` carries it,
the corpus's `invalid_causes` counts it, and the trace invariant's `revoked_principal`
exemption reads it. The vocabulary is listed once, in `journal.md`'s admission section, and
the v2 model refuses an `invalid` result whose `error.type` is outside it.

## CLI and corpus

- `ledgergate keygen`, `ledgergate sign`, `ledgergate journal id`, `ledgergate journal
  principal {add,revoke,list}`, `ledgergate journal approver {add,revoke,list}` (each
  pre-checks the registry and reports a typo as a refusal, exit `2`; the trigger behind it is
  the guarantee, as check 4's `UNIQUE` is, so an operator error is never reported as
  corruption); `journal pending` lists each pending operation's admitted approvers and which
  are live (a read-only listing that rebuilds the set from the definition's
  `policy_configuration`, as `verify` already does; `serve`'s refusal to rebuild is about
  *writing* under a set the operator did not name); `create`
  gains `--approver NAME=KEYFILE` (replacing `--approval-key`); `approve`'s `--signing-key`
  becomes `--seed-file`, and its existing `--approver` is checked against the registry; `initialize` returns `journal_id` in `_meta`.
- Corpus grammar: `setup.approvers: [{name, seed}]` (published test seeds) replaces
  `setup.approvals`; `setup.principals: [{name, seed}]` seeds signed principals; a step's
  `sign: {approver: NAME, ...}` picks the approver seed (the wrong-approver scenario signs
  with a registered-but-not-allowed one); a step's `sign_as: NAME` wraps it in an envelope
  with that principal's seed (an unregistered `sign_as` name is a corpus fault); `sign_as_seed:
  SEED` alongside `sign_as` substitutes the signing seed, which is how the unregistered-key
  scenario signs a registered name with a key the registry does not hold.
  Three new scenarios: a signed request applied (`correct/`), a request signed with a key not
  registered for its principal, and an approval by a registered approver the line does not
  admit (`red-team/`), each expecting the containing mechanism through two expectation keys the corpus grammar
  gains for it, `invalid_causes` and `approval_verdicts` (`corpus.md`), so `bad_signature: 1`
  and `approval_wrong_approver: 1` are what the expectations say, not merely `invalid: 1`.

## Amendments to earlier documents (made in this change; the remainder at implementation)

- `journal.md` (made): admission input gains `auth`; write step 4 and read step 4 name the
  expiry and replay checks at the single reading; approval check 1 reads the registry, check
  1b added, `approvers_for` among the guarded policy calls; the tables section gains the two
  registry tables, the `invocations` attribution columns and the new verdict; capacity
  formula; the clone limit's owner is M8c.
- `mcp-runtime.md` (made): step 4 forwards `params._meta.ledgergate` as `auth`; the capacity
  formula gains the registry terms (`journal.md` owns it); the
  single-principal statements become "one *transport* principal per session; any number of
  signed ones"; `initialize` carries `journal_id` in `result._meta.ledgergate` (read from the
  definition at start, no journal transaction); `--approval-key` becomes `--approver`.
- `trace-v2.md` (made): the additive fields, the two events, the invariant, the verdict.
- `corpus.md` (made): `setup.approvals` becomes `setup.approvers`; `setup.principals`,
  `sign_as`, `sign.approver`.
- ADR-0002 §3 body: authentication and approver identity are M8a; the network listener M8b;
  multi-tenancy is *not* claimed by any M8 row (one journal is one tenant; several tenants are
  several journals, and nothing here changes that).
- `README.md`: the clone-limit sentence names M8c.

## What this document does not claim

- **A network transport.** Nothing listens. Signed requests are transport-independent so that
  M8b can be a thin listener that forwards and adds nothing the journal trusts.
- **Confidentiality.** Signatures authenticate; they do not encrypt.
- **Cross-clone consumption authority.** The clone limit stands; M8c. The replay set is the
  same kind of guarantee, a SQLite-local `UNIQUE` per writable file lineage: a captured signed
  request is applicable once in each writable clone, the same operator rule applies (one
  writable copy), and the same M8c authority would close it.
- **Key custody or rotation.** The journal holds verification keys; seeds are the operator's.
- **Recomputation of a request signature from a trace.** Stored as evidence, not re-derivable.
- **Hiding which names are registered.** `unknown_principal` and `bad_signature` are distinct
  causes, so a caller on a stdio session can learn whether a name is registered. The session
  is the process owner's; a listener (M8b) decides whether to collapse them.
- **Client libraries.** The signed `call_id` is derived from the JSON-RPC `id`
  (`mcp-runtime.md`), so a signer must control the `id` its client sends, and must keep its
  call ids unique *per principal per journal*, not per session: a second session that restarts
  its ids at 1 meets `replayed_call` on every reused one, which is the mechanism working.
  Many MCP client libraries assign ids. A client that cannot is M8b's concern (a listener could accept a
  client-chosen call id in the envelope), not M8a's.
