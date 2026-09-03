# Spec: trace schema v2 and journal derivation

Normative specification for the M3 trace format decided in
[ADR-0002](../adr/0002-runtime-surface-and-plan.md). Schema v1
([`schema/trace/v1.json`](../../schema/trace/v1.json)) is frozen; it remains the offline
ingest format. v2 is what the runtime derives from the [journal](journal.md).

## Units

An **intent** is a decoded command or read, identified before anything decides on it. A
**disposition** is what the runtime did with the invocation. Every invocation yields
exactly one `invocation_resolution`; it yields an intent only if admission succeeded.

## Event grammar per invocation

```
tool_call
  [command_intent | read_intent]   iff disposition != invalid
  invocation_resolution            exactly one; disposition, operation ref, attempted digest
                                   disposition: new | replay | conflict | approval | read | invalid | legacy
  [policy_decision]                iff disposition in {new, approval} or a policy-gated read
  [ledger_command -> ledger_result] iff (a policy_decision == allow on a write intent)
                                       or disposition == legacy
  [read_result]                    iff disposition == read and no policy_decision == deny
tool_result
```

- `replay` and `conflict`: no decision, no ledger pair; `invocation_resolution` names the
  operation resolved to, whose original decision and pair appear earlier in the same
  trace (a trace is derived from a whole journal; in a windowed export the reference is
  marked `external`).
- `deny` / `approval_required`: the intent ends at its decision.
- `approval`: the decision carries the approval reference and verdict; if `allow`, the
  ledger pair follows.
- `invalid`: `tool_call`, `invocation_resolution` (`invalid`), `tool_result` (error). No
  intent, no operation, no decision. Applies identically to write and read tools.
- `read`: `read_intent`, resolution, optional decision. If no decision or the decision is
  `allow`: `read_result` with the journal position observed, head, and result digest. If
  the decision is `deny`: no `read_result`; the `tool_result` carries the denial. The
  disposition is `read` in both cases.
- `legacy`: a v1 pair lifted on import. Its `ledger_command`/`ledger_result` carry the same
  reference semantics as a runtime write's; there is no decision to reference.

Cardinality and order are rules of the schema description, enforced by the models as
v1's are.

## Identifiers

Derived identifiers are decimal, positive, prefixed, and must pass `require_identifier`:

- `intent_id`: `intent-<invocation journal_sequence>`
- `command_id`: `command-<operation journal_sequence>`
- `call_id`: taken from the `events` row (tokenized).

`seq` is the dense enumeration of emitted events in `(journal_sequence, intra-row
ordinal)` order. Top-level `chart` and `currencies` come from `definition`.

## v1 documents

A v1 document is lifted into the v2 model on read. Each v1 `ledger_command`/`ledger_result`
pair becomes an intent with disposition **`legacy`** and **no `policy_decision`**: v1 has
no policy evidence, and inventing an `allow` would be exactly the synthesized decision
this design forbids. The ledger pair replays as before; policy checks over a `legacy`
intent report "no evidence" rather than "allowed". The runtime reads v1 and v2, derives v2
from the journal, and never derives v1.

## Status

The v2 schema, models, lift and replayer are M3 deliverables. This document is their
contract; nothing here exists in code yet.
