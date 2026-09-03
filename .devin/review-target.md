# Review target

- Branch `plan/adr-0002-revision`, revision 15, HEAD `889680a` (or a later tooling-only commit on the same branch).
- Scope: `docs/adr/0002-runtime-surface-and-plan.md`, `docs/spec/*.md`, README rows; code: `src/ledgergate/ledger/` (state.py, entries.py, identifiers.py), `src/ledgergate/trace/`, `schema/trace/v1.json`, `pyproject.toml`.
- Your previous pass (on 881ed50, revision 14) found P1-1 (presentation row cannot carry a verdict decided after it), P2-1 (request_digest had no column), P2-2 (two envelope digests), P2-3 (command codec has no permitted layer), and P3s. Revision 15 claims:
  - P1-1: `approvals` row holds artefact fields plus the pure-check result of checks 1-3 (`checks_passed | approval_invalid | approval_expired | approval_scope_mismatch | approval_not_applicable`), written after checks 1-3 and before check 4; the final `approval_verdict` (`approval_valid | approval_already_used | check result`) is on `decisions`; trace-v2 takes the verdict from decisions.
  - P2-1: `invocations.request_digest` added (null for invalid); trace attempted digest defined per disposition (attempted_fingerprint for writes, request_digest for reads, input_digest for invalid).
  - P2-2: envelope records `input_digest`; wire-level correlation not offered.
  - P2-3: `ledgergate.codec` below trace and journal; trace.models delegates; round-trip fingerprint invariant; contract `cli -> runner -> {invariants, report, derive} -> {trace, journal} -> codec -> ledger`.
  - P3s: `approval_not_applicable` is not a failed verdict and never short-circuits policy; allocator gap wording; rebuild wording (core re-validates; a raise on an applied fold is the step-2 integrity failure; no policy re-evaluated); per-disposition crash analysis; AUTOINCREMENT; M4 is transport only; pending operations expire with a retired journal; read decisions use request_digest; M2c admitter also returns approval_unsupported; history count removed.
- Verify closure by tracing the text; look for anything revision 15 newly broke or that remains; say whether M2b can be built without further document changes.
