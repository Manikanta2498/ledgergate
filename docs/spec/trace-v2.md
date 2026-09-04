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
  [approval_presentation]          3   iff the resolution carries a presentation_ref: the
                                       presentation row (check result, verified, bindings,
                                       approver identity only once the signature verified)
  [policy_decision]                4   iff disposition in {new, approval}, or a read whose tool the
                                       configured policy set declares gated (the null set gates none)
  [ledger_command                  5   iff a policy_decision == allow on a write intent
   ledger_result]                  6
  [read_result]                    7   iff disposition == read and no policy_decision == deny
tool_result                      8
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
  whose original decision and pair appear earlier in the same trace (a trace is always
  derived from a whole journal, under one read snapshot, so every reference resolves, and
  the model enforces it: a `new` creates a fresh operation and a fresh outcome; a `replay`,
  `conflict` or `approval` names an operation an earlier `new` created; a `replay` or a
  failed-verdict `approval` names the outcome that was that operation's current one at the
  time; an `approval`, whatever its verdict, is against an operation whose current outcome is
  pending; a produced outcome is produced exactly once, in allocation order; a `replay` never
  carries a presentation against a pending operation, since that is an `approval`). The model also
  ties every command intent to what it is about: the fingerprint of its `command` is its
  `attempted_digest`, equals the operation's for `new`, `replay` and `approval` and differs
  for `conflict`, and equals the command its `ledger_command` carries, whose `command_id`
  is the operation and whose `call_id` is the intent's; and in a runtime trace every
  `tool_call` and `tool_result` brackets an intent. The registry additionally requires each
  presentation and each consumption to be referenced by at most one decision, an `approval`
  the policy set decided to have consumed a valid artefact, any other disposition's verdict to
  be `approval_not_applicable`, and a consumption to be recorded after its presentation.
- `deny` / `approval_required`: the intent ends at its decision.
- `approval` with a failed verdict: no outcome was appended, so `invocation_resolution`
  names the operation's pending tip, the outcome that was *current* at the time (the latest
  one produced before this resolution, and one whose decision was `approval_required`),
  exactly as a `replay` names the current outcome; the `policy_decision` carries the `runtime.approval_rejected` rule and
  the verdict.
- `approval`: the decision carries the approval presentation reference and verdict; if
  `allow`, the ledger pair follows.
- `invalid`: `tool_call`, `invocation_resolution` (`invalid`), `tool_result` (error). No
  intent, no operation, no decision. Applies identically to write and read tools. The
  `tool_call`'s `arguments` is the empty object: the input was not admitted, the envelope's
  redacted payload stays in the journal, and nothing of it is carried into a trace.
- `read`: `read_intent`, resolution, optional decision. If no decision or the decision is
  `allow`: `read_result` with the journal position observed, head, and result digest. If
  the decision is `deny`: no `read_result`; the `tool_result` carries the denial. A read's
  decision is never `approval_required`: a read has no operation to approve, and a policy
  set that returns it for a read is a configuration fault the journal refuses unrecorded.

**Tool boundary.** Every runtime intent (every disposition but `legacy`) is bracketed by
its own boundary events: the event immediately before its first event is its `tool_call`
and the event immediately after its last is its `tool_result`, with the same `call_id`, and
nothing else is interleaved within the intent's events (a standalone message sits at its
own row's sequence, never inside an invocation's ordinals).
`call_id` is not unique across a trace (a caller may retry with the same one); the
bracketing is what ties a boundary pair to its intent. Lifted v1 content keeps v1's rule
(one `tool_result` per `tool_call`, after it) and is not bracketed.

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
| `context` | the canonical serialized `PolicyContext`, verbatim: principal, subject (nullable), command digest and `digest_kind`, evaluation time, `policy_set_version`, the command's kind, amount and currency (decimal string; nullable), every historical aggregate value the rules read, and the approval `{presentation, verdict}` if one was presented |
| `approval` | presentation reference and the decision's `approval_verdict`, when one was presented (the verdict is taken from `decisions`, not from the presentation row, which holds only the pure-check result) |
| `consumption` | consumption reference, when one was kept |

A consumer with the policy set at `policy_set_version` can recompute `decision` from
`context` and compare. A consumer without it can verify only that the recorded evidence is
internally consistent, and must say which of the two it did.

## Legacy grammar (v1 documents lifted on read)

v1 tool events and ledger pairs are not one-to-one: one `tool_call` may be followed by
several ledger commands, or by none. Lifting each ledger pair into a full runtime
invocation would require inventing `tool_call`/`tool_result` events that never happened.
Lifted content therefore uses its own grammar and never synthesizes boundary events. A
document is *derived* iff it carries a `journal_id`, and then has no `legacy` resolution and
every boundary event brackets an intent; otherwise it is *lifted*, carries only `legacy`
resolutions (possibly none: a v1 document may hold tool events or messages alone) and is not
bracketed. The partition is by producer, not by content, so a runtime document's grammar can
never be switched off by lifted rows:

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

- `intent_id`: `intent-<invocation journal_sequence>`; in a derived document the model enforces
  this grammar, that intent numbers strictly increase along the trace, and that every row an
  invocation wrote (its produced outcome, its presentation, its consumption) has a sequence
  strictly between the invocation's and the next invocation's, and a `new`'s operation, the
  first row of its transaction, a sequence between the previous invocation's and its own,
  which is what the journal's single sequence and serialized transactions guarantee; so the
  numbers the read and consumption checks compare are witnessed, not chosen
- `command_id`: `command-<operation journal_sequence>` (model-enforced grammar in a derived
  document)
- `outcome_ref`: `outcome-<outcome journal_sequence>` (on `invocation_resolution`); the model
  enforces this grammar and, for produced outcomes, allocation order (each produced outcome's
  number exceeds the previous one's)
- `presentation_ref`: `presentation-<approvals journal_sequence>` (on `invocation_resolution`
  and `policy_decision.approval`; required on an `approval` disposition, which is defined by a
  presented artefact); model-enforced grammar
- `consumption_ref`: `consumption-<approval_consumptions journal_sequence>`; model-enforced
  grammar, present exactly when the verdict is `approval_valid` (a registry row checks it,
  and that every failed verdict was decided by the runtime)
- `call_id`: taken from the `events` row (tokenized). For an `invalid` invocation whose
  `call_id` was not recoverable, `invalid-<invocation journal_sequence>`; its `tool` is
  `unknown` when the envelope kept none; its `attempted_digest` is the envelope's
  `input_digest`.
- a standalone `message` carries the time the journal recorded it (kept in its row).
- lifted v1 content: `intent_id` is `legacy-<v1 seq of the ledger_command>` (bounded by
  position, since a v1 `command_id` may already use the whole identifier length),
  `operation_id` is the v1 `command_id`, and `attempted_digest` is the JCS digest of the
  command document (not the core fingerprint: a v1 document may record a command the core
  refused to construct, and the digest must be computable for every valid v1 document);
  re-checked by the model on load. A v1 document without `ended_at` lifts with the latest
  event time.

`seq` is the dense enumeration of emitted events in anchored order: `(invocation
journal_sequence, ordinal)` for runtime content, `(v1 seq, ordinal)` for lifted content,
as defined in their grammars above. Top-level `chart` and `currencies` come from `definition`.

## Ledger pairs and intents

`ledger_command` and `ledger_result` keep v1's shape and carry no `intent_id`. A
`ledger_command` belongs to the intent whose events immediately precede it in anchored
order (its own invocation's, by construction of the ordinals); its `ledger_result` belongs
to the same intent by `command_id`, however far away it sits (lifted v1 results may be
separated from their commands by other v1 events). Replay of a v2 document is the v1
replayer over the ledger pairs alone (`TraceV2.ledger_view()`); nothing else in v2 replays.

## Invariants and verification

`ledgergate verify <trace-or-journal>` derives (from a journal) or loads (a v1 document is
lifted) a v2 trace and runs the invariant registry (`ledgergate.invariants.REGISTRY`) over
it. Each invariant is a pure function of the trace grounded in a named document, and reports
`pass`, `fail`, or `no_evidence`: a trace that does not carry what an invariant would need
(a lifted v1 trace for the policy invariants, a chartless trace for replay) is reported as
such and never as a pass. Several registry rows restate rules the v2 model also enforces at
load; a document violating them fails to load rather than failing a row, and the scorecard
then records that the loaded document satisfies them. The registry is the statement of what
is checked; the validator is one of its mechanisms. Two rows check reads: every `read_result`
head equals the most recent recorded `ledger_result` head (or genesis) and its cursor equals
the largest outcome any earlier resolution referenced, since every outcome is named by the
resolution that produced it and that resolution precedes any later read, so a stale or
premature projection fails; and its `result_digest` is the JCS digest of the value the
caller was served in the `tool_result`, so the served value is bound to the row (agreement
of that value with the replayed books is not checked). A further row checks that the *committed response*
(the outbound event the journal committed; not proof of delivery, see journal.md
`invocation_responses`) is what the journal did, per the decision-to-outcome tables: success iff a
read was not denied or the ledger applied, otherwise the error type of the path taken and,
on a decided path, the decision's rule and reason as the message; an applied write's served
head, sequence and entry equal to the ledger result's and a rejected write's served error
equal to the ledger result's; and a replay told exactly what the producing invocation
was told (the same result with `replayed` set, or the same error verbatim).
The scorecard is the combined result and is itself tri-state: `fail` if any invariant
failed, `pass` only if none failed *and at least one ran*, otherwise `no_evidence`. The
process exits 0 for `pass`, 1 for `fail`, 3 for `no_evidence`, 2 when the source could not
be read; a trace that carries nothing any invariant quantifies over is reported, never
passed.

## Recomputation and the policy configuration

The top level carries `policy_config_digest` (the definition's) and, when the set is
declarative, `policy_configuration`, the JCS document the digest is over; the model requires
the two to agree and every decision to name the trace's set. `PolicyDecision.context` is a
typed `PolicyContextDoc`, not an open object: every field the journal persists, with fixed
grammars, and the model requires its verdict and presentation to agree with the decision's.
A registry row (`decision_recomputes`) first recomputes every set-derived input in the
persisted context from the trace itself: the subject from the command intent
(`transaction_id`, or none), and each aggregate `applied.<kind>.<CCY>.<W>s` as the sum of the
applied ledger commands of that kind, currency and subject produced by earlier intents whose
requested time lies within the window ending at the evaluation time; a context whose subject
or aggregates the trace does not support fails. It then re-runs the configuration over the
context and requires the recorded decision, rule and reason; it needs a configuration for a set whose
rules are wholly declarative (`ThresholdPolicySet`, `NullPolicySet`) and reports
`no_evidence` for a subclass or a custom set, whose rules are code. `runtime.` rules are a
closed registry (`runtime.approval_rejected`); any other is refused at load.

## Presentations

An `approval_presentation` event (ordinal 3, between the resolution and the decision)
carries the presentation row: `presentation_ref`, `verified`, `check_result`, the presented
`journal_id` and `fingerprint`, the timestamps, and the approver identity fields exactly when
the signature verified. Exactly one exists iff the resolution carries a `presentation_ref`,
and a decision after a presentation carries its verdict, so every reference to a presentation
resolves to typed evidence, on every disposition.

## Boundary binding

The boundary call *is* the intent, and the model checks it: an `invalid` call carries the
empty arguments, no idempotency key and no presentation; a read call carries the read's tool
and arguments and no key; a write call, with its idempotency key, decodes to the intent's
command. Every later intent against an operation (`replay`, `conflict`, `approval`) carries
the key that created it, since the fingerprint excludes the key by design.

## Limits

Journal admission enforces every bound the trace has on what the journal admits or serves:
the payload bound (10,000 nodes, depth 32) on tool arguments, 1,000 postings per entry,
1,024 characters for descriptions, tag keys and values, 100 tags per entry, 65,536
characters per message; `create` refuses a chart whose trial balance would not fit the
payload bound or an account name over 1,024 characters; and before any row is written the
journal refuses, as unrecorded configuration faults, an error message, rule or reason over
1,024 characters and a policy set's subject or aggregates outside the grammars the context
carries. So every admitted input, every served result and every persisted context is
representable here; `events` is bounded at 5,000,000 and derivation is
whole-journal. Segmentation of a journal that outgrows that bound is not designed; it is
the M4 runtime's first design question, stated in ADR-0002.

## Status

The v2 schema (`schema/trace/v2.json`, generated from the models and checked against them
under test), models, lift, derivation, invariant registry and `ledgergate verify` ship in
M3. The runtime reads v1 and v2, derives v2 from the journal, and never derives v1.
