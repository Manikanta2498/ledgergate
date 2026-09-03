# Review target

- Branch `plan/adr-0002-revision`, revision 14 (HEAD after commit "revision 14").
- Scope: ADR-0002, docs/spec/*.md, README rows; code: src/ledgergate/ledger/ (state.py, entries.py, identifiers.py), src/ledgergate/trace/, schema/trace/v1.json, pyproject.toml.
- Your previous pass (on 3d79ea3) found P1-1 and P2-1..4 plus P3s. Revision 14 claims:
  P1-1: under M2b a Request with non-null approval fails admission (`approval_unsupported`, disposition invalid); approvals tables tested empty via that path; artefact format is M3.
  P2-1: admission input is an untyped JSON value; `Request` is admission's output; two named digests, input_digest (envelope) and request_digest; neither is the operation fingerprint.
  P2-2/3: M2b adds public `ledgergate.ledger.command_fingerprint(command)`, used by Ledger.execute and the journal; PolicyContext.command_digest := operations.fingerprint.
  P2-4: rebuild folds applied from recorded command + effects, never re-decides; awaiting_approval/denied/rejected are no-ops that advance the cursor.
  P3s: gap wording; journal.kind enforced by per-table trigger; not_applicable row placement for conflict/read, invalid has none; audit-survives qualifier; presentation ref on invocation_resolution; test wording; derivation layer `ledgergate.derive`; history count; ADR-0001 supersession.
- Verify closure by tracing; look for anything newly broken; say whether M2b can be built without further document changes.
