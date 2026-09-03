# Review target

- Branch `plan/adr-0002-revision`, revision 21 (HEAD is the commit titled "revision 21" or a later tooling-only commit).
- Scope as before.
- Your previous pass (8129c54, revision 20) found P2-A (duplicate member names pass; JCS cannot catch them) and P2-B (null-policy subject undefined), plus P3s. Revision 21 claims: object_pairs_hook rejecting repeated names added to the transport list and to the unrecorded-failure list, with the note that the JCS serializer cannot see duplicates so the M2b harness uses the hook; subject is nullable, null under the null set, derivation declared by policy sets in M3; approvals row display fields no longer "bound", marked nullable; digest_kind added to the trace-v2 context enumeration and ADR; roadmap M3 wording; arguments-integers vs results-strings asymmetry stated; codec layer wording.
- Verify closure; look for anything newly broken; say whether M2b can be built without further document changes.
