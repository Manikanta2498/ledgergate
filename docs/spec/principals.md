<!--
SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
SPDX-License-Identifier: Apache-2.0
-->

# Authenticated principals and named approvers (M8a)

ADR-0002 §3: "a mandate without an authenticated principal is not a mandate; the server
refuses a network transport until then." Today every invocation is attributed to one
transport-trusted principal (`local`, or the `--principal` name a stdio operator chooses), and
an approval artefact's `approver` is a label the signer wrote, verified against a single
journal-wide key, so the label says who *claims* to have approved, not who did. M8a makes both
facts authenticated, without a network listener: a **principal registry** and **signed
requests**, so an invocation's principal is the holder of a registered key; and an
**approver registry**, so an artefact's approver is the holder of the key registered under
that name, and a policy line can require a named approver. The network transport (M8b) and
external execution with an outbox (M8c) are split out and not claimed here; M8a is what they
both need first, and it is testable end to end over stdio.

## What changes and what does not

- The journal's write and read protocols, admission, policy evaluation, dispositions,
  outcomes and the trace grammar are unchanged in shape. M8a adds *who* to what is already
  recorded: an `authentication` on every invocation, and a registry the definition binds.
- Every M4–M7 journal still opens and derives; a journal without registries behaves as
  before (one transport principal; the definition's single approval key, if any, accepts any
  approver label), and the trace says so.
- Nothing here moves money or listens on a port.

## Principals

A **principal** is an identifier (the existing grammar) with an authentication kind:

| Kind | Who it is | How an invocation is attributed to it |
| :-- | :-- | :-- |
| `transport` | the operator of the process | as today: `serve --principal NAME` (default `local`); the process owner is trusted because they own the process |
| `signed` | the holder of an Ed25519 key registered under the name | the request carries a signature over its canonical content, verified before admission |

The **principal registry** is a journal table, `principals`: `principal` (`UNIQUE`), `kind`,
`verification_key` (base64url Ed25519, null for `transport`), `added_at`, `added_by`
(the principal that ran the command), `revoked_at` (null while live). Entries are appended by
`ledgergate journal principal add PATH NAME --key-file FILE` and `... revoke PATH NAME`, each
an event in the journal's own log (a row with a `journal_sequence`, so a derived trace shows
when a principal existed), not an edit to the definition. Revocation is monotone: a revoked
name cannot be re-added (a new key is a new name), so "who was this at sequence *n*" has one
answer.

A journal created without `--principal-registry` has an implicit registry of one
`transport` principal, the definition's `principal`, exactly as today. A journal created with
it (or any journal after its first `principal add`) **requires** every write and read to be
attributed to a live registry entry; a `transport` invocation is accepted only if
`--principal` names a live `transport` entry, and a request signed by an unknown or revoked
key is `invalid`.

### Signed requests

A signed request is the MCP `tools/call` with `params._meta.ledgergate` set to

```json
{"principal": "treasury-agent", "expires_at": "2026-09-06T12:00:30+00:00", "signature": "<base64url>"}
```

The signature is Ed25519 over `canonical_bytes` (JCS) of

```json
{"journal_id": "<the journal's>", "call_id": "<the call's, as serve derives it>",
 "tool": "...", "arguments": {...}, "key": "..." | null, "approval": {...} | null,
 "principal": "treasury-agent", "expires_at": "..."}
```

that is, exactly the value `serve` hands the journal (`mcp-runtime.md`, step 4) plus the
envelope, bound to one journal (a signature for another journal verifies nowhere else) and
to one call id. The journal verifies in **admission**, before any decode of the command
(`journal.md`, *Admission*): the envelope is well-formed (shape, else `invalid:
authentication_malformed`); `principal` names a live `signed` entry (else `invalid:
unknown_principal`); the signature verifies under that entry's key over the recomputed
canonical bytes (else `invalid: bad_signature`); `expires_at` is after the invocation's
`requested_at` (the single clock reading, else `invalid: request_expired`); and `(principal,
call_id)` has not been seen (else `invalid: replayed_call`; a signed message replayed
verbatim is refused, while a legitimate retry is a *new* call with the *same idempotency
key*, which the write protocol answers as it always has). Every refusal is an `invalid`
disposition with the cause in `error_type`, recorded like every other invalid call and
spending nothing. Verification happens once, in the journal, whatever the transport: a
future network listener (M8b) forwards the message and adds nothing the journal trusts.

A `transport` principal presents no envelope; an envelope on a call from a `transport`
session is `invalid: unexpected_authentication` (one principal per invocation, never two
claims). A `signed` principal's request on a stdio session is accepted regardless of
`--principal`: the signature, not the session, is the attribution. The invocation row
records `principal` (the authenticated one) and `authentication` = `transport` or
`signed`; `PolicyContext.principal` is the authenticated one, so a policy line can name it.

### Keys

Keys are Ed25519, as approvals already are; `ledgergate keygen --out FILE` writes a seed and
prints the verification key; the seed never enters the journal. Compromise is handled by
revocation and a new name. Key rotation without a new name is not offered: a name with two
keys over time makes "who signed this" a question about the clock, and the clock is the
signer's.

## Approvers

The **approver registry** is a second table, `approvers`: `approver` (`UNIQUE`),
`verification_key`, `added_at`, `added_by`, `revoked_at`, appended by `ledgergate journal
approver add PATH NAME --key-file FILE` and `... revoke`. Approval check 1 (`journal.md`,
*Approval protocol*) becomes: the artefact's `approver` names a live registry entry *and* the
signature verifies under **that entry's key**; otherwise `approval_invalid`. The approver
label is therefore authenticated: it names the key that signed, and `ledgergate approve
--key-file` signs with the seed whose verification key is registered under the label it
writes (it refuses to write a label whose registered key is not the seed's).

**Legacy.** A journal whose definition carries an `approval_key` and whose `approvers` table
is empty treats that key as registered under *every* label (today's behaviour, stated). The
first `approver add` ends that: from then on only registered approvers verify, and the
definition key verifies nothing unless it is also registered under a name. A derived trace
records which rule applied (`approval_presentation.authenticated`: `true` for a registry
match, `false` for the legacy key).

**Policy.** `ThresholdPolicySet.approve_above` lines gain an optional `approvers: [names]`.
When present, an artefact whose (authenticated) approver is not in the list fails a new
check, **1b**, verdict `approval_wrong_approver`, a runtime-written deny like the other
failed verdicts (`RUNTIME_RULES` grows by one; `decision_recomputes` learns it). An
`approvers` list naming a name not in the registry is a configuration fault at `open`, like
a policy that can require approval without any key. The configuration digest covers the list,
so a changed list is a changed policy, refused at `open` unless the same set is given.

## Trace v2

Additive, optional fields, no grammar change to existing documents:

- `invocation_resolution.authentication`: `"transport"` | `"signed"`; absent in a lifted or
  pre-M8a document.
- `approval_presentation.authenticated`: `true` | `false`; absent before M8a.
- Two new event types, `principal_change` and `approver_change` (`name`, `kind` or key, `action`
  `add` | `revoke`, `by`), in the invocation order of the log so a verifier can compute the
  live registry at any sequence.
- A new invariant, `attributions_are_registered`: every `signed` invocation names a
  principal live at its sequence, every `true` presentation an approver live at its sequence,
  every change event's `by` a principal live at its sequence. `no_evidence` for documents
  without registry events.

`invalid` causes gain `authentication_malformed`, `unknown_principal`, `bad_signature`,
`request_expired`, `replayed_call`, `unexpected_authentication`; `error_type` on the
`tool_result` carries the cause as today.

## MCP surface

- `serve` accepts signed requests on any session; no new flag. `--principal` continues to
  name the transport principal for unsigned calls; on a registry journal it must name a live
  `transport` entry, else `serve` refuses to start (exit `2`).
- New CLI: `ledgergate keygen`, `ledgergate journal principal {add,revoke,list}`, `ledgergate
  journal approver {add,revoke,list}`, `ledgergate sign` (signs a request value from a file
  with a seed: what a client library would do, so the corpus and the tests can produce
  signed requests without a client). `journal approve` gains `--key-file` (replacing the
  ad-hoc key argument) and refuses a label the seed is not registered under.
- The corpus gains two red-team scenarios (a request signed by an unregistered key; an
  approval by the wrong approver) and one correct scenario (a signed request applied), each
  with expectations naming the containing mechanism; the scenario grammar gains
  `setup.principals`, `setup.approvers`, and a step-level `sign_as: NAME` that the runner
  turns into an envelope with the scenario's published test seeds.

## What this document does not claim

- **A network transport.** Nothing listens. Signed requests are transport-independent so that
  M8b can be a thin listener; that listener, its TLS, its session handling and its rate
  limits are M8b's design.
- **Confidentiality.** Signatures authenticate; they do not encrypt. A stdio session is as
  private as the process; anything else is the transport's job.
- **Cross-journal consumption authority.** The clone limit (`journal.md`, invariant 3) stands;
  an approver registry does not coordinate clones. M8c.
- **Key custody.** Where seeds live is the operator's problem; the journal holds only
  verification keys.
