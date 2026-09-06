<!--
SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
SPDX-License-Identifier: Apache-2.0
-->

# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow PEP 440 and are
the package's `__version__`. The trace and result schemas carry their own versions.

## [Unreleased]

### Added
- Journal schema 7: principal and approver registries, Ed25519-signed requests verified in
  admission with expiry and replay refusal, named approvers on policy lines
  (`approval_wrong_approver`), attribution on every invocation, admission cause codes as the
  served error type, `attributions_are_registered` (M8a).

### Changed
- `ledgergate serve --approval-key` is `--approver NAME=KEYFILE`; `ledgergate approve
  --signing-key` is `--seed-file`; a schema-6 journal is refused (re-create).

## [0.1.0a1] - 2026-09-06

The first alpha: everything from milestones M0 to M7, as recorded in the merged pull requests.

### Added
- The deterministic double-entry ledger core: money, accounts, entries, the transaction
  lifecycle, idempotent commands with fingerprint conflicts, a hash chain (M1).
- Trace schema v1 and v2, the journal-to-trace derivation, and `ledgergate verify` over the
  invariant registry (M2a, M3).
- The durable SQLite command journal with a bound definition, single-use signed approvals and
  the tokenizing, redacting admitter (M2b, M2c, M3).
- The threshold policy set with deny lines, approval lines, window caps and gated reads (M3).
- `ledgergate serve`, the stdio MCP runtime (M4).
- `ledgergate record --from-otel`, the OpenTelemetry GenAI observational adapter (M5).
- The scenario and red-team corpus, `ledgergate run`, `result.json`, `ledgergate report`
  (md, JUnit, SARIF, drift) (M6).
- Conformance levels, the mutation ratchet, CodeQL and Scorecard scanning, and the
  provenance-attested release pipeline (M7).
