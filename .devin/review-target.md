# Review target

- Branch `plan/adr-0002-revision`, revision 16 (HEAD is the commit titled "revision 16" or a later tooling-only commit).
- Scope: ADR-0002, docs/spec/*.md, README rows; code: src/ledgergate/ledger/, src/ledgergate/trace/, schema/trace/v1.json, pyproject.toml.
- Your previous pass (76f0dc4, revision 15) found P2-1..4 and P3-1..6. Revision 16 claims:
  P2-1 approval_verdict domain now enumerates approval_not_applicable and null; presentation ref non-null whenever a presentation exists.
  P2-2 command_digest is the fingerprint for writes and request_digest for reads, with digest_kind in the context.
  P2-3 crash analysis: committed approval retries as replay iff verdict was approval_valid, else fresh approval.
  P2-4 ADR §4 reworded: policy_decision appears when the invocation was decided (policy set or runtime), never on replay.
  P3-1 derive is an optional layer in the M2b import-linter contract.
  P3-2 check 4 does a SELECT before allocating, so a used approval leaves no allocator row; UNIQUE remains the constraint.
  P3-3 "check result" at every presentation-row site.
  P3-4 canonical JSON is RFC 8785.
  P3-5 check 3 compares fingerprint and key only; subject/amount/currency are display fields, recorded not compared.
  P3-6 README diagram says schema v1 | v2.
- Verify closure by tracing; look for anything newly broken; say whether M2b can be built without further document changes.
