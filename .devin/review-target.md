# Review target

- Branch `plan/adr-0002-revision`, revision 17 (HEAD is the commit titled "revision 17" or a later tooling-only commit).
- Scope: ADR-0002, docs/spec/*.md, README rows; code as before.
- Your previous pass (aa13bd2, revision 16) found P2-A (RFC 8785 misattributed: JCS makes 5.0 == 5; json.dumps is not JCS), P2-B ("policy-gated read" undefined), P3-A (message rows are two-row transactions), P3-B (signed scope unstated), P3-C (projection on rollback unstated). Revision 17 claims each is closed with a sentence at the cited site: JCS semantics stated correctly, whole-float refusal is pre-digest model validation, M2b vendors a JCS serializer tested against the RFC vectors; policy-gated defined as declared by the policy set, null set gates none, stated in both journal.md and trace-v2; message transaction wording; signature covers all artefact fields per RFC 8785; rollback keeps the prior projection reference and discards the executed Ledger value.
- Verify closure; look for anything newly broken; say whether M2b can be built without further document changes.
