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
`signed` add, null otherwise), `by` (the principal whose invocation appended it), `at` (the
transaction's single clock reading). A `CHECK`-and-trigger rule makes the log monotone per
name: exactly one `add`, at most one `revoke`, and never an `add` after a `revoke` (a new key
is a new name, so "who was `treasury-agent` at sequence *n*" has one answer, computed by
walking the log to *n*). A name is **live at *n*** iff its `add` is at or before *n* and no
`revoke` is. `approver_events` is the same table shape for approvers (`kind` absent).

Registries are **not** part of the definition: adding or revoking a name changes no digest
and is not a binding check at `open`; it is history, like an invocation. The one link to the
definition is the bootstrap: `create` writes the definition and, in the same transaction, the
first event, `add` of the transport principal named by `--principal` (default `local`), with
`by` equal to itself, the single stated exemption from "`by` is live at its sequence": the act
of creating the journal is the operator's own attribution. `create --approver NAME=KEYFILE`
(repeatable) appends approver adds the same way, and the existing `create`-time rule "a
policy that can require approval needs someone who can approve" now reads: `approve_above`
non-empty requires at least one approver seeded, and every name in any line's `approvers`
(below) must be seeded. Later commands append: `ledgergate journal principal add PATH NAME
--verification-key-file FILE` (`signed`; a `transport` add takes no key), `... revoke PATH
NAME`, and `ledgergate journal approver add|revoke ...`. Each is an invocation of the
principal running the command (`--principal`, or a signed request through `serve`; the CLI's
own principal must be live), so `by` is always a recorded, attributed actor.

### Attribution of every invocation

Every invocation row carries `principal` and `authentication`, and every `open` and every
transaction holds the registry rule:

- `serve --principal NAME` (and every CLI command that writes) refuses to start unless `NAME`
  is a live `transport` principal (exit `2`); if `NAME` is revoked *during* a session (another
  process appended the revoke), each later call is recorded `invalid: revoked_principal` under
  the write lock, never a silent success and never an unrecorded refusal.
- A request **without** an `auth` member is a transport invocation: `principal` = the session's,
  `authentication` = `transport`.
- A request **with** an `auth` member is a signed invocation: attributed by the signature, not
  the session (a signed request over any stdio session is accepted; the session's principal
  is not consulted). `principal` = the envelope's, `authentication` = `signed`, once verified.
- A request whose `auth` member fails is recorded `invalid` with the cause below;
  `principal` = **the session's transport principal** (who delivered it; the claimed name is
  caller text and is never stored, per `identifiers-and-redaction.md` §4: it resolves to a
  registry name or it is content), `authentication` = `rejected`.

`PolicyContext.principal` is the authenticated principal, so a policy line can name it.

### The `auth` envelope

The admission input (`journal.md`, *Admission input and Request*) gains one optional member,
`auth`; `serve` forwards exactly one `_meta` member, `params._meta.ledgergate`, as it
(`mcp-runtime.md`, step 4, amended: `_meta` is otherwise still not forwarded). Its shape:

```json
{"principal": "treasury-agent", "expires_at": "2026-09-06T12:00:30+00:00", "signature": "<base64url Ed25519>"}
```

The signature is over `canonical_bytes` (JCS) of the **request as delivered** with the
signature removed:

```json
{"journal_id": "<the journal's>", "call_id": "<the call's, as serve derives it>",
 "tool": "...", "arguments": {...}, "key": "..." | null, "approval": {...} | null,
 "principal": "treasury-agent", "expires_at": "..."}
```

so a signature cannot be moved between journals, calls, tools, arguments, keys or artefacts,
and `expires_at` is covered. A client learns `journal_id` from `initialize`'s
`result._meta.ledgergate.journal_id` (added; `_meta` is where MCP puts such things) or from
`ledgergate journal id PATH`.

**Where each check runs.** Admission is pure and clockless, so it does what needs no clock:
`auth` present but not this shape → `invalid: authentication_malformed`; `principal` not a
live `signed` principal at the current head → `invalid: unknown_principal` (a `transport`
name or a revoked name is unknown *as a signer*); signature does not verify over the
recomputed bytes → `invalid: bad_signature`. Admission runs on the pre-tokenization value
(`admission.py` already sees the raw request), which is the value the client signed. The two
checks that need the clock run at the protocol's **single reading**: in the write protocol at
step 4 and in the read protocol at its own reading (`journal.md`), `expires_at <=
requested_at` → `invalid: request_expired`, and `(principal, call_id)` already present among
*verified signed* invocations → `invalid: replayed_call` (a replayed message is refused; a
legitimate retry is a *new* call with the *same idempotency key*, which the write protocol
answers as it always has; a rejected envelope never enters the replay set, so a forged
`(victim, X)` cannot block the victim's later `X`). The one-reading rule is untouched.

**What is stored.** A verified signed invocation persists `auth_principal`, `auth_expires_at`
and `auth_signature` on its row, like a presentation persists an artefact; the signature is
evidence of *who*, not something a later verifier can recompute (arguments and call ids are
tokenized before storage, so the signed bytes are gone by design), and the trace carries
`authentication` and `principal`, not the signature.

### Keys

Ed25519, as approvals already are. `ledgergate keygen --seed-file FILE` writes a seed (mode
0600) and prints the verification key; a seed never enters a journal. Compromise is handled by
`revoke` and a new name; rotation under one name is not offered (two keys over time under one
name makes "who signed this" a question about the clock, and the clock is the signer's).
`ledgergate sign --seed-file FILE --journal-id ID REQUEST.json` produces the envelope for a
request value, what a client library would do, so tests and the corpus can produce signed
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
unchanged, and includes it when present, so a changed list is a changed policy. The policy
protocol gains one **pure** method, `approvers_for(context) -> frozenset[str] | None`: the
names the line that would require approval for this context admits, or `None` for any
approver (the null set and lines without the field return `None`; the same first-match walk
as `evaluate`, so the answer is the line `evaluate` would name). The journal calls it,
guarded like every policy call, as **check 1b**, after check 1 and before consumption (check
4): a verified artefact whose authenticated approver is not in the set is the failed verdict
`approval_wrong_approver`, decided by the runtime as the other failed verdicts are
(`runtime.approval_rejected`, reason = the verdict; nothing is consumed; the operation stays
pending). The closed verdict vocabulary grows by that one value in `approvals.check_result`,
`decisions.approval_verdict` and v2's `Verdict`, part of the schema-7 change. The existing
rule that a policy asking for approval *after* a valid artefact was consumed is a
configuration error stands: the wrong-approver case never reaches consumption, so it cannot
trip it.

A line naming an approver later revoked is not a fault (the registry is history, the
definition is not): the line can no longer be satisfied and its operations stay pending until
the operator adds an allowed approver, which `journal pending` shows.

## Trace v2 (additive)

- `invocation_resolution` gains `principal` (Identifier) and `authentication`
  (`transport` | `signed` | `rejected`), derived from the invocation row, present in every
  schema-7 derivation and absent in lifted or pre-M8a documents (optional fields).
- `policy_decision.context.approval` gains `approver` (the authenticated name when check 1
  passed, else `null`), so a verifier can recompute `approvers_for` and check that
  `approval_wrong_approver` was the right verdict and that `approval_valid` was allowed by the
  line; `decision_recomputes` does exactly that for `ThresholdPolicySet` contexts (a runtime
  rule is still never recomputed as a *policy* decision; this is recomputing the input it
  keyed on).
- Two event types, `principal_change` and `approver_change` (`name`, `action`, `kind`, `by`,
  `at`), at their `journal_sequence` position, so a verifier computes liveness at any sequence.
- One invariant, `attributions_are_registered`: every `signed` resolution's principal is live
  at its sequence; every verified presentation's approver (`context.approval.approver`) is
  live at its sequence; every change event's `by` is live at its sequence, except the first
  event of the trace when it is the bootstrap `add` of a transport principal by itself.
  `no_evidence` for a document without change events (a lifted v1, an earlier v2).
- The v2 capacity bound (`journal.md`, *Segmentation*; `mcp-runtime.md`) counts registry
  events alongside invocations and null-invocation events; the formula is amended.

`invalid` causes gain `authentication_malformed`, `unknown_principal`, `bad_signature`,
`request_expired`, `replayed_call`, `revoked_principal`; `error_type` on the `tool_result`
carries the cause as today.

## CLI and corpus

- `ledgergate keygen`, `ledgergate sign`, `ledgergate journal id`, `ledgergate journal
  principal {add,revoke,list}`, `ledgergate journal approver {add,revoke,list}`; `create`
  gains `--approver NAME=KEYFILE` (replacing `--approval-key`); `approve` gains `--seed-file`
  and `--approver`; `initialize` returns `journal_id` in `_meta`.
- Corpus grammar: `setup.approvers: [{name, seed}]` (published test seeds) replaces
  `setup.approvals`; `setup.principals: [{name, seed}]` seeds signed principals; a step's
  `sign: {approver: NAME, ...}` picks the approver seed (the wrong-approver scenario signs
  with a registered-but-not-allowed one); a step's `sign_as: NAME` wraps it in an envelope
  with that principal's seed (an unregistered `sign_as` name is a corpus fault; the
  unregistered-key scenario uses a registered name with a *different* seed, `sign_as_seed`).
  Three new scenarios: a signed request applied (`correct/`), a request signed with a key not
  registered for its principal, and an approval by a registered approver the line does not
  admit (`red-team/`), each expecting the containing mechanism (`invalid: bad_signature`,
  `runtime.approval_rejected` with `approval_wrong_approver`).

## Amendments to earlier documents (made in this change)

- `journal.md`: admission input gains `auth`; the write protocol's step 4 and the read
  protocol name the expiry and replay checks at the single reading; approval check 1 reads the
  registry, check 1b added; schema 7 tables; capacity formula; the clone limit's owner is M8c.
- `mcp-runtime.md`: step 4 forwards `params._meta.ledgergate` as `auth`; the single-principal
  statements become "one *transport* principal per session; any number of signed ones";
  `initialize` carries `journal_id`.
- `trace-v2.md`: the additive fields, the two events, the invariant, the verdict.
- ADR-0002 §3 body: authentication and approver identity are M8a; the network listener M8b;
  multi-tenancy is *not* claimed by any M8 row (one journal is one tenant; several tenants are
  several journals, and nothing here changes that).
- `README.md`: the clone-limit sentence names M8c.

## What this document does not claim

- **A network transport.** Nothing listens. Signed requests are transport-independent so that
  M8b can be a thin listener that forwards and adds nothing the journal trusts.
- **Confidentiality.** Signatures authenticate; they do not encrypt.
- **Cross-clone consumption authority.** The clone limit stands; M8c.
- **Key custody or rotation.** The journal holds verification keys; seeds are the operator's.
- **Recomputation of a request signature from a trace.** Stored as evidence, not re-derivable.
