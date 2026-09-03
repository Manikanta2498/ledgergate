# Spec: identifiers, tokenization and redaction

Normative specification for M2c, decided in
[ADR-0002](../adr/0002-runtime-surface-and-plan.md). Everything here happens **at
admission, before the ledger hashes anything**, so every digest is computed over the
stored form and a trace replays exactly.

## Three classes of field

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
   ledger definition, stored as given. The definition loader warns on values that look
   like emails, phone numbers or card numbers; the operator owns what they name their
   accounts.

Amounts, currencies, sides and account references remain in the clear; they are the books.

## Token format

The raw value is first validated by `require_identifier` (non-empty, single line, at most
256 characters). The token is

```
tk1_<domain>_<base64url(HMAC-SHA256(key, domain || 0x00 || raw))>
```

with no padding (43 digest characters), a domain of at most 32 `[a-z0-9-]` characters, and
a fixed `tk1` version prefix: at most 80 characters of `[A-Za-z0-9_-]`, validated once more
after construction. The token domain and key version are in `definition`; rotating the key
means a new journal, and cross-journal correlation is an explicit operation.

## Scope

M2c covers schema v1 fields. v2's intent and policy fields are designed under the same
three classes in M3.
