# Spec: identifiers, tokenization and redaction

Normative specification for M2c, decided in
[ADR-0002](../adr/0002-runtime-surface-and-plan.md). Everything here happens **at
admission, before the ledger hashes anything**, so every digest of admitted content is
computed over the stored form and a trace replays exactly. The one digest of *rejected*
content, the failure envelope's `input_digest`, is necessarily over the raw input; it is
therefore keyed under the token key (the admitter's `digest_input`), so it commits to the
input without being reversible by dictionary.

## Four classes of field

1. **Free text** (`description`, message `content`, tool `arguments` and `result`, tags
   (keys *and* values: both are the caller's), account `name` in the definition, metadata
   keys and values): fail-closed redaction. A field not on the allowlist is redacted. Replacement
   tokens are deterministic (keyed HMAC), so equal inputs redact equally across runs. The
   empty string redacts to itself: there is nothing to protect, and the ledger treats `""`
   as "no description"; this reveals that a field was empty and nothing else.
2. **Caller-supplied identifiers** (`transaction_id`, idempotency keys, `call_id`,
   `trace_id`, subject identifiers in the `PolicyContext`): tokenized, on **every
   reference**, before the `PolicyContext` is built, before the command is fingerprinted,
   before the ledger looks anything up, before any row is written. `open_transaction`
   stores the token of a `transaction_id`; a later `settle` with the same raw id tokenizes
   to the same value and finds it. A retry with the raw key tokenizes to the same key.
   Replay operates only on stored tokens and needs no key.
3. **Operator-defined identifiers** (`account_id`, tool names): configuration in the
   ledger definition, stored as given. `Journal.create` warns (Python `warnings`) on an
   account id that looks like an email address or a run of ten or more digits; the operator
   owns what they name their accounts.
4. **References to runtime-generated identifiers** (`entry_id` in a `reverse`): the
   caller repeats an id the ledger issued. Validated by `require_identifier` at admission,
   never tokenized, because tokenizing it would make every reference resolve to nothing.
   A reference is free of caller content only once it *resolves*: until the ledger confirms
   it issued that id, it is arbitrary caller text. Admission therefore resolves it against
   the current projection (inside the transaction, after the cursor check), and an unknown
   one is an admission failure (`unknown_entry`, disposition `invalid`, key not spent), so
   the raw reference exists only inside the redacted envelope. The recorder refuses such a
   `reverse` before recording anything, and that refusal propagates to the caller (including
   through `Recorder.run`, which tolerates only *recorded* failures): a refused attempt is
   never silently absent from a trace that then certifies itself consistent.

Amounts, currencies, sides and account references remain in the clear; they are the books.
An account reference is operator-defined only once it *resolves* against the chart; before
that it is caller text, so both paths (journal admission and the recorder) check every
posting's account against the chart before the core sees the command, and an unknown one is
refused with nothing recorded.

## Token format

The raw value is first validated by `require_identifier` (non-empty, single line, at most
256 characters). The token is

```
tk1_<domain>_<base64url(HMAC-SHA256(key, domain || 0x00 || raw))>
```

with no padding (43 digest characters), a domain matching `^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$`
(one to 32 characters, no leading or trailing hyphen, never empty, so domain separation is
always meaningful), and a fixed `tk1` version prefix: between 49 and 80 characters of
`[A-Za-z0-9_-]`, validated once more after construction. The token domain and key version are in `definition`; rotating the key
means a new journal, and cross-journal correlation is an explicit operation.

A free-text replacement has the parallel form `rd1_<domain>_<base64url(HMAC-SHA256(key,
domain || ":text" || 0x00 || text))>`; the `:text` purpose suffix (a domain cannot contain
`:`) separates it from the identifier space, so a value used as both an identifier and a
description yields two unrelated outputs. Both forms are checked against their grammar
after construction. The keyed input digest is lower-case hex
`HMAC-SHA256(key, domain || 0x00 || "input" || 0x00 || JCS(input))`, and the key check
value is `base64url(HMAC-SHA256(key, domain || ":keycheck" || 0x00))` without padding.

## Scope and mechanism

M2c covers the journal's admission `Request` and schema v1 documents *as the recorder
produces them* (an externally produced v1 trace cannot be redacted after the fact and still
replay, since its heads were computed over the raw form), through one implementation: `ledgergate.codec.Tokenizer` (key, domain, key version) performs every
transform, and two thin adapters call it. `ledgergate.journal.TokenizingAdmitter` is the
journal's `Admitter`: it transforms the `arguments` document field by field before the
codec decodes it, so the decoded command, its fingerprint, the request digest and every
row are over the stored form. `ledgergate.trace.Recorder(redactor=tokenizer)` transforms
each runtime `Command` before it is recorded *or executed*, and each message, tool call and
tool result before it is appended, so the recorded heads and fingerprints are over the
stored form and the trace replays exactly with no key. The two adapters are held to one
test: transforming the JSON document and transforming the runtime command yield the same
stored command.

In a `Request`, `arguments` is class-1 content whose allowlist is the command's own field
classes: amounts, currencies, sides and account references in the clear; `description` and
tag values redacted; caller identifiers tokenized; the `entry_id` of a `reverse` validated
and kept. A field of the wrong type, or a tag key the core would refuse (empty or untrimmed), is left
for the codec to refuse structurally, so the two admitters share every shape rule. A
non-finite number in an untyped tool payload is refused before recording on both paths. In a v1
trace, tool `arguments` and `result` are untyped JSON and are redacted fail-closed: every
string, whether an object key or a leaf, and every number is replaced by a redaction token
(a card number is a number as often as a string); booleans, null and structure are kept.
Nothing replays a tool payload, so nothing depends on the original values. `trace_id`, `call_id`
and idempotency keys are tokenized; `scenario_id` and the agent descriptor are operator
configuration and stay as given; metadata keys and values are redacted. A tool name is
operator configuration only if the operator declared it (`Recorder(tools=...)`): under a
redactor, an undeclared tool name is redacted, since a hallucinated name is caller text.

**What is deliberately not redacted.** The core's own error messages (a ledger result's
`error.message`, the journal's `outcomes.error_message`). On both paths the core only ever
sees the transformed command, with every account and entry reference already resolved, so a
message can name a token, a chart account or a ledger-issued entry id but never raw caller
content, and replay recomputes and compares the message byte for byte; a second
transformation would make every recorded rejection diverge on replay.

**Key handling.** The key is at least 16 bytes, supplied by the operator, held only by the
`Tokenizer`, never written to the journal, a trace or a log; its `repr` shows the domain and
key version only. A journal records the token domain, the key version label, and a **key
check value** `HMAC(key, domain || ":keycheck" || 0x00)`, which identifies the key without
revealing it (not reversible for a random key of at least 16 bytes). `open` refuses an
admitter whose key does not reproduce it, because a different key under the same label
would fork the identifier space silently: the same raw idempotency key would apply twice
and a `settle` would miss the transaction its `open` stored. The label alone cannot detect
that; the check value does.

**Approval artefact fields.** An artefact is issued by the approver against what the
journal already holds, so its `key` and `subject` are the stored tokens, and admission does
not transform them again (re-tokenizing would break the equality check 3 depends on). Every
field is nonetheless bounded by a fixed grammar at admission before anything is stored,
verified or not: `journal_id` 32 hex, `fingerprint` 64 hex, `signature` 86 base64url
characters, `amount` a decimal string of at most 40 digits, `currency` three capitals,
`approval_id`, `approver`, `key` and `subject` identifiers. An artefact that fails the
grammar is `approval_malformed` at admission and never reaches a presentation row; an
artefact that passes it can carry nothing unbounded or free-form.

v2's intent and policy fields are designed under the same four classes in M3.
