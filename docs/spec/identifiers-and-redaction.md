# Spec: identifiers, tokenization and redaction

Normative specification for M2c, decided in
[ADR-0002](../adr/0002-runtime-surface-and-plan.md). Everything here happens **at
admission, before the ledger hashes anything**, so every digest of admitted content is
computed over the stored form and a trace replays exactly. The one digest of *rejected*
content, the failure envelope's `input_digest`, is necessarily over the raw input; it is
therefore keyed under the token key (the admitter's `digest_input`), so it commits to the
input without being reversible by dictionary.

## Four classes of field

1. **Free text** (`description`, message `content`, tool `arguments` and `result`, tag
   values, account `name` in the definition): fail-closed redaction. A field not on the
   allowlist is redacted. Replacement tokens are deterministic (keyed HMAC), so equal
   inputs redact equally across runs.
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
   Generated ids carry no caller content by construction. The definition loader warns on values that look
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

## Scope

M2c covers schema v1 documents and the journal's admission `Request`. In a `Request`,
`arguments` is class-1 content whose allowlist is the command's own field classes: it is
redacted field by field (amounts, currencies, sides and account references
in the clear; `description` and tag values redacted; caller identifiers tokenized), which
are the same classes v1 command documents have. v2's intent and policy fields are designed
under the same three classes in M3.
