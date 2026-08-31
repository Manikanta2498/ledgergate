# LedgerGate

**A correctness-enforcing ledger runtime for autonomous agents that move money, plus the
invariant conformance suite that proves an agent respects financial state machines before
deployment.**

> **Status: pre-alpha, milestone M0.** The scaffold, gates and architecture are in place.
> The ledger core lands in M1. Nothing here is usable yet. See [Roadmap](#roadmap).

---

## The problem

Fintechs are putting LLM agents in front of refunds, disputes and billing. LLMs are
non-deterministic, and four failure modes follow:

- **Idempotency failures.** A tool call times out, the agent retries without a key, and a
  refund pays out twice.
- **Ledger invariant violations.** A cross-currency conversion fails to balance across
  asset and liability accounts, leaking money silently.
- **Illegal state transitions.** The agent jumps `PENDING` straight to `REFUNDED`,
  skipping `SETTLED`.
- **Behavioral drift.** A prompt tweak or a model swap raises the error rate on edge
  cases, and nothing catches it.

You cannot check any of this with another probabilistic prompt. LLM-as-a-judge cannot tell
you whether debits equal credits. That is an assertion, not an opinion.

## The approach

Deterministic invariants over a versioned execution trace.

```
agent run ──▶ trace (schema v1) ──▶ invariants ──▶ result.json ──▶ md | junit | sarif
                    ▲
       adapters: anthropic | openai | langgraph
```

The interop contract is `schema/trace/v1.json`, not our harness. If your agent emits the
schema, the corpus runs against it, whatever framework you use.

## Design commitments

These are enforced by CI gates, not by convention:

| Commitment | Enforced by |
| :--- | :--- |
| The ledger core is deterministic | `scripts/check_determinism.py` fails on any `random`/`uuid` import or wall-clock call in `src/ledgergate/ledger/` |
| The dependency graph stays one-way | `import-linter` contracts in `pyproject.toml` |
| The ledger core stays pure | `import-linter` forbidden contract: no `json`, `pathlib`, `sqlite3`, no adapters |
| No accidental network in tests | `pytest --disable-socket` by default |
| Money is never a float | integer minor units, checked in review and by type |

## Roadmap

| Milestone | Contents | Status |
| :--- | :--- | :--- |
| **M0** | Repo, licensing, toolchain, gates, ADR-0001 | **done** |
| M1 | Deterministic ledger core, property and stateful tests | next |
| M2 | Trace schema v1, SQLite idempotency, fail-closed redaction | |
| M3 | Invariant registry, corpus, CLI, first scorecard | |
| M4 | Agent runner, adapters, recorded cassettes | |
| M5 | Full corpus, SARIF/JUnit, security workflows, mutation gate | |
| M6 | Release, drift table, conformance levels | |

## Development

```bash
uv sync --all-groups   # install
make check             # every gate CI runs
make fmt               # format and autofix
```

Individual gates: `make lint`, `make types`, `make imports`, `make determinism`,
`make test`, `make cov`, `make audit`.

## Licensing

Source-available, split deliberately:

- `corpus/` and `schema/` are **Apache-2.0**. Adopt, redistribute and cite them freely,
  including in production.
- Everything else is **BUSL-1.1**. Read it, modify it, run it in development, CI and
  evaluation. Production use requires a commercial license. Converts to Apache-2.0 on
  2030-08-31.

See [LICENSING.md](LICENSING.md) and [COMMERCIAL.md](COMMERCIAL.md).
