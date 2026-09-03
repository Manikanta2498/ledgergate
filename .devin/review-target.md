# Review target

- Branch `plan/adr-0002-revision`, revision 18 (HEAD is the commit titled "revision 18" or a later tooling-only commit).
- Scope as before.
- Your previous pass (aa83891, revision 17) found P1-A (JCS cannot digest unbounded integers; input_digest undefined on the envelope path), P2-A (artefacts not bound to a journal; single use per database only), P3-A (denied read indistinguishable in invocation_responses), P3-B ("canonical command" undefined). Revision 18 claims: admission input is I-JSON by contract, enforced by the transport at decode, violation is an unrecorded transport error listed under failures; amounts bounded to the JCS-safe range by codec and definition, artefact amount too; definition gains a random 128-bit journal_id, signed into artefacts and compared in check 3, invariant 3 and ADR reworded; denied gated read writes response = denied; operations.command described as codec storage form, not a digest input.
- Verify closure; look for anything newly broken; say whether M2b can be built without further document changes.
