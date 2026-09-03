# Spec: trace schema v2 and journal derivation

Normative specification for the M3 trace format decided in
[ADR-0002](../adr/0002-runtime-surface-and-plan.md). Schema v1
([`schema/trace/v1.json`](../../schema/trace/v1.json)) is frozen; it remains the offline
ingest format. v2 is what the runtime derives from the [journal](journal.md).

## Units

An **intent** is a decoded command or read, identified before anything decides on it. A
**disposition** is what the runtime did with the invocation. Every invocation yields
exactly one `invocation_resolution`; it yields an intent only if admission succeeded.

## Event grammar per runtime invocation

```
tool_call                        ordinal 0
  [command_intent | read_intent]   1   iff disposition != invalid
  invocation_resolution            2   exactly one; disposition, operation ref, the exact outcome
                                       that answered this invocation (from invocation_responses),
                                       attempted digest (= attempted_fingerprint for writes,
                                       request_digest for reads, input_digest for invalid),
                                       approval presentation ref if one was presented
                                       disposition: new | replay | conflict | approval | read | invalid
  [policy_decision]                3   iff disposition in {new, approval}, or a read whose tool the
                                       configured policy set declares gated (the null set gates none)
  [ledger_command                  4   iff a policy_decision == allow on a write intent
   ledger_result]                  5
  [read_result]                    6   iff disposition == read and no policy_decision == deny
tool_result                      7
```

**Ordering.** Every event derived from one invocation is placed at
`(invocation.journal_sequence, ordinal)`, regardless of which journal row its data comes
from. This is deliberate: the immediate foreign keys force the `invocations` row to be
written before the inbound `events` row it owns, so sorting by source-row sequence would
put `command_intent` before `tool_call`. Standalone `message` events sit at
`(event.journal_sequence, 0)`. `seq` is the dense enumeration over that order.

- `replay` and `conflict`: no decision, no ledger pair; `invocation_resolution` names the
  operation resolved to and, for `replay`, the exact outcome that answered (so a retry
  that was told `awaiting_approval` says so even if the operation was approved later),
  whose original decision and pair appear earlier in the same trace (a trace is derived from a whole journal; in a windowed export the reference is
  marked `external`).
- `deny` / `approval_required`: the intent ends at its decision.
- `approval`: the decision carries the approval presentation reference and verdict; if
  `allow`, the ledger pair follows.
- `invalid`: `tool_call`, `invocation_resolution` (`invalid`), `tool_result` (error). No
  intent, no operation, no decision. Applies identically to write and read tools.
- `read`: `read_intent`, resolution, optional decision. If no decision or the decision is
  `allow`: `read_result` with the journal position observed, head, and result digest. If
  the decision is `deny`: no `read_result`; the `tool_result` carries the denial.

Cardinality and order are rules of the schema description, enforced by the models as
v1's are.

## `policy_decision` payload

Offline re-evaluation runs the same policy code on the same inputs; the event therefore
carries the inputs, not a summary of them:

| Field | Content |
| :--- | :--- |
| `intent_id` | the intent judged |
| `policy_set_version` | which rules ran (`none` for the M2b null policy) |
| `decision` | `allow`, `deny`, `approval_required` |
| `matched_rule`, `reason` | the rule that decided, and why. A `runtime.` prefix (`runtime.approval_rejected`) means the runtime wrote the decision without invoking the policy set, and a consumer must not attempt to recompute it from policy code |
| `context` | the canonical serialized `PolicyContext`: principal, subject, command digest, evaluation time, and every historical aggregate value the rules read |
| `approval` | presentation reference and the decision's `approval_verdict`, when one was presented (the verdict is taken from `decisions`, not from the presentation row, which holds only the pure-check result) |
| `consumption` | consumption reference, when one was kept |

A consumer with the policy set at `policy_set_version` can recompute `decision` from
`context` and compare. A consumer without it can verify only that the recorded evidence is
internally consistent, and must say which of the two it did.

## Legacy grammar (v1 documents lifted on read)

v1 tool events and ledger pairs are not one-to-one: one `tool_call` may be followed by
several ledger commands, or by none. Lifting each ledger pair into a full runtime
invocation would require inventing `tool_call`/`tool_result` events that never happened.
Lifted content therefore uses its own grammar and never synthesizes boundary events:

```
legacy_intent              intent_id, command, optional call_id from the v1 ledger_command
invocation_resolution      disposition: legacy; operation ref = the v1 command_id
ledger_command
ledger_result
```

**Ordering of lifted content.** A v1 document's own `seq` is the anchor, since it is
already strictly increasing. Each v1 `ledger_command` at v1 sequence *s* yields
`legacy_intent` (0), `invocation_resolution` (1), `ledger_command` (2) at `(s, ordinal)`,
and its paired v1 `ledger_result` at v1 sequence *r* yields `ledger_result` at `(r, 0)`.
v1 `tool_call`, `tool_result` and `message` events pass through unchanged at
`(their v1 seq, 0)`. The v2 `seq` is the dense enumeration over that order, so a lifted
trace is deterministic for any interleaving of v1 tool, message and ledger events, and no
event moves relative to another. A `legacy_intent` has no `policy_decision`: v1 carries no
policy evidence, and an invented `allow` would be exactly the synthesized decision this
design forbids. Policy checks over `legacy` report "no evidence", not "allowed". The
ledger pair replays as before.

## Identifiers

Derived identifiers are decimal, positive, prefixed, and must pass `require_identifier`:

- `intent_id`: `intent-<invocation journal_sequence>`
- `command_id`: `command-<operation journal_sequence>`
- `call_id`: taken from the `events` row (tokenized).

`seq` is the dense enumeration of emitted events in anchored order: `(invocation
journal_sequence, ordinal)` for runtime content, `(v1 seq, ordinal)` for lifted content,
as defined in their grammars above. Top-level `chart` and `currencies` come from `definition`.

## Status

The v2 schema, models, lift, derivation and replayer are **M3** deliverables. M2b builds
the journal without deriving traces from it; this document is the contract M3 is built to,
and nothing in it exists in code yet. The runtime will read v1 and v2, derive v2 from the
journal, and never derive v1.
