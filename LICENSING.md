# Licensing

LedgerGate is **source-available, not open source**. The split is deliberate.

| Region | License | What you may do |
| :--- | :--- | :--- |
| `corpus/`, `schema/` | **Apache-2.0** (OSI approved) | Anything, including production and commercial use. These are the scenario corpus and the trace schema, and they are meant to be adopted, redistributed and cited freely. |
| `src/ledgergate/` and everything else | **BUSL-1.1** (not OSI) | Read, copy, modify, redistribute, and use **non-production**: evaluation, development, testing, research, education, personal use. Production use requires a commercial license. |

**Change Date: 2030-08-31.** On that date the runtime converts automatically to
Apache-2.0. This applies per version, so each release carries its own four-year clock.

## Why the split

The scenario corpus and trace schema only have value if they spread. A team on LangGraph
or the raw OpenAI SDK should be able to adopt the schema, run the corpus and cite the
results without asking anyone. Those are Apache-2.0 for that reason.

The runtime is the part worth paying for, so it is BUSL-1.1: fully readable, freely
evaluable, and licensed for production.

## What counts as production

The BUSL grants non-production use. Reading the code, running the test suite, running
LedgerGate against your own agents in a development or CI environment, benchmarking it,
and writing about it are all non-production and require no license from anyone.

Running it as part of a system that authorizes, records or settles real money, or
offering it to third parties as part of a product or service, is production. See
[COMMERCIAL.md](COMMERCIAL.md).

## Notes

- BUSL-1.1 and Apache-2.0 are both listed by SPDX. The BUSL is not OSI approved, so
  GitHub will not display it as an open-source license, which is expected.
- BUSL covenant 1 requires the Change License to be GPL-compatible. Apache-2.0 is
  compatible with GPL-3.0, satisfying "GPL Version 2.0 or a later version".
- Per-file SPDX identifiers are enforced in CI so the boundary is never ambiguous.
