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

### Fixed (external whole-repository review, 2026-09-06)
- `advance` with event `refund` is refused at the codec and the core; it used to commit a
  rejected row that made the journal non-derivable.
- Corpus setup binding compares each setup step's whole behaviour and the starting balances,
  not the attempted fingerprint alone.
- Release: production publication only on a tag *push*; the already-published check fails
  closed; the build backend is pinned and locked; an installed-wheel gate runs on every PR.
- Verifier: approval evidence transitions and logical approval-id uniqueness, the
  cause/authentication matrix, reads bound to their principal, replay not bounded by the v1
  document limit, lossless legacy digests, codec bounds as a replay finding, calendar-edge
  timestamps as validation errors.
- Journal (schema 8): `INSERT OR REPLACE` refused by a trigger on every connection, a schema-7 journal refused like every earlier one; rendered ledger error
  messages bounded rather than classified as corruption; verification keys stored canonically.
- CLI: `verify --emit-trace` refuses to alias its source; `sign` reads bounded I-JSON;
  `journal pending` reports `unknown` for a custom set; `approve` bounds `--valid-hours` and
  never echoes a seed.
- Mutation baseline regeneration keeps known flakiness (source digest, `retire-flaky`).
- Licensing: `docs/` and `scripts/` are Apache-2.0, `tests/` BUSL-1.1, stated; the package
  metadata declares `license = "BUSL-1.1"`.

- Verifier findings are a checked contract (`Finding`: row name, closed severity, non-empty
  message, identifier `intent_id` when given; `check` refuses a row's finding it does not own);
  the mutation baseline is regenerated from the runner's results (516 unkilled of 2,165, two
  equivalents with reasons, one flaky).
- README states the supported procedure for a journal of an earlier schema, including the
  journals no build can derive (`journal dump` is their row-level export).

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
