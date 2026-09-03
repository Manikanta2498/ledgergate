# Review target

- Branch `plan/adr-0002-revision`, revision 20 (HEAD is the commit titled "revision 20" or a later tooling-only commit).
- Scope as before.
- Your previous pass (aa3c06f, revision 19) found P2-1 (result_digest over bytes not stored) and P2-2 (codec amount bound has no consumer and would break frozen v1), plus P3s. Revision 20 claims: tool results carry Money amounts as decimal strings as returned and as stored, so result_digest = SHA-256(JCS(stored result)); the codec bound is withdrawn (storage form, nothing digests it, transport bounds inputs, v1 unaffected), ADR codec invariant restated over every command the core accepts; JCS serializer lives in ledgergate.codec; artefact display fields nullable, signed as JCS null; "no consumption allocator row"; aggregates also read from operations; parse_float checks finiteness; README "single-use (per journal)".
- Verify closure; look for anything newly broken; say whether M2b can be built without further document changes.
