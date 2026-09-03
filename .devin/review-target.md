# Review target (written by the orchestrating agent before invoking spec-review)

- Branch: `plan/adr-0002-revision`; expected HEAD: `da5a0e9` or later on that branch.
- Scope: `docs/adr/0002-runtime-surface-and-plan.md`, `docs/spec/journal.md`,
  `docs/spec/trace-v2.md`, `docs/spec/identifiers-and-redaction.md`, README roadmap and
  design-commitments rows. Code to verify against: `src/ledgergate/ledger/` (state.py,
  identifiers.py, entries.py), `src/ledgergate/trace/`, `schema/trace/v1.json`,
  `pyproject.toml` import-linter contracts.
- This is revision 13. Your previous pass (on d68dd64) found seven P2s and eleven P3s.
  Revision 13 claims to close all of them:
  1. failed verdict: runtime writes the decision (`runtime.approval_rejected`), policy set
     not invoked; 2. valid approval x approval_required is a runtime fatal config error,
  not "rejected at definition load"; 3. approval presented on other dispositions gets a
  presentation row with verdict `approval_not_applicable`; 4. `rejected` spends the key in
  the journal, divergence from the core stated in Terms, ADR and README, one fingerprint
  function; 5. `journal` allocator table as the single counter, gaps permitted;
  6. canonical `Request` envelope; 7. README corpus milestone fixed to M6.
  P3s: rule 5 restated as implied by the schema; UNIQUE(journal_sequence, operation);
  events.invocation nullable for messages; envelope mentioned in events row; `response`
  semantics defined; COALESCE aligned; `ledgergate approve` placed in M3; approval key
  `none` in M2b; pending-table rows are M3 tests; `ledgergate.journal` placed in the layer
  contract.
- Verify each is actually closed by tracing the text, then look for anything revision 13
  newly broke or that remains. State whether M2b can be built without further document
  changes.
