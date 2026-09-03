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
   values): fail-closed redaction. A field not on the allowlist is redacted. Replacement
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
   ledger definition, stored as given.
4. **References to runtime-generated identifiers** (`entry_id` in a `reverse`): the
   caller repeats an id the ledger issued. Validated by `require_identifier` at admission,
   never tokenized, because tokenizing it would make every reference resolve to nothing.
   A reference is free of caller content only once it *resolves*: until the ledger confirms
   it issued that id, it is arbitrary caller text. Admission therefore resolves it against
   the current projection (inside the transaction, after the cursor check), and an unknown
   one is an admission failure (`unknown_entry`, disposition `invalid`, key not spent), so
   the raw reference exists only inside the redacted envelope. The recorder refuses such a
   `reverse` before recording anything. The definition loader warns on values that look
   like emails, phone numbers or card numbers; the operator owns what they name their
   accounts.

Amounts, currencies, sides and account references remain in the clear; they are the books.

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

## Scope and mechanism

M2c covers schema v1 documents and the journal's admission `Request`, through one
implementation: `ledgergate.codec.Tokenizer` (key, domain, key version) performs every
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
and kept. A field of the wrong type is left for the codec to refuse structurally. In a v1
trace, tool `arguments` and `result` are untyped JSON and are redacted fail-closed: every
string leaf is replaced, structure and non-string leaves are kept. `trace_id`, `call_id`
and idempotency keys are tokenized; `scenario_id` and the agent descriptor are operator
configuration and stay as given; metadata values are redacted.

**What is deliberately not redacted.** The core's own error messages (a ledger result's
`error.message`, the journal's `outcomes.error_message`). The core only ever sees the
transformed command, so a message can name a token or an operator-defined account id but
never raw caller content, and replay recomputes and compares the message byte for byte;
a second transformation would make every recorded rejection diverge on replay.

**Key handling.** The key is at least 16 bytes, supplied by the operator, held only by the
`Tokenizer`, never written to the journal, a trace or a log; its `repr` shows the domain and
key version only. A journal records the token domain, the key version label, and a **key
check value** `HMAC(key, domain || ":keycheck" || 0x00)`, which identifies the key without
revealing it (not reversible for a random key of at least 16 bytes). `open` refuses an
admitter whose key does not reproduce it, because a different key under the same label
would fork the identifier space silently: the same raw idempotency key would apply twice
and a `settle` would miss the transaction its `open` stored. The label alone cannot detect
that; the check value does.

v2's intent and policy fields are designed under the same four classes in M3.
