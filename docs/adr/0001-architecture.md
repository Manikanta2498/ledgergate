# ADR 0001: Hexagonal core, injected effects, schema as the interop contract

- Status: Accepted
- Date: 2026-08-31

## Context

LedgerGate makes three claims that only hold if the architecture enforces them:

1. Replay is reproducible to the hash, which is what makes CI cost nothing and makes
   model-drift comparisons meaningful rather than noise.
2. The corpus is framework-agnostic, so a team on LangGraph or the raw OpenAI SDK can
   run it without adopting our harness.
3. Idempotency is exactly-once under retry storms.

An earlier draft of the design placed `ledger/`, `runner/` and `report/` as top-level
packages, described the corpus as framework-agnostic "because it contains no Python"
while also placing `.py` invariants inside it, and described the storage layer as ACID
without naming a backend that provides those guarantees.

## Decision

**Src-layout for the runtime.** Everything importable lives under `src/ledgergate/`.
Top-level `ledger` and `report` packages are already taken on PyPI and would collide on
`sys.path`. Src-layout additionally means tests exercise the installed wheel rather than
the working tree, so a packaging mistake fails CI instead of failing a user.

**The corpus and schema are not package data.** `corpus/` and `schema/` stay at the
repository root, outside the wheel. They are Apache-2.0 and the runtime is BUSL-1.1;
vendoring the former into the latter's distribution would blur exactly the boundary the
license split exists to draw, and would force every adopter who only wants the schema to
take the BUSL artifact to get it. The runtime therefore locates them by explicit path,
and they are published separately when they stabilize. The cost is that a corpus run
needs a path argument rather than working from a bare `pip install`; that is the correct
trade for a contract meant to outlive this implementation.

**The ledger core is pure; effects are injected.** `Clock`, `IdGenerator` and
`FxRateSource` are Protocols supplied by the caller. The core is a function of
`(state, command)`. `scripts/check_determinism.py` parses the ledger package and fails CI
on any import of `random`, `secrets` or `uuid`, or any call to `datetime.now`,
`time.time` and friends.

**The interop contract is a versioned trace schema, not the absence of Python.**
`schema/trace/v1.json` (JSON Schema 2020-12) defines a normalized execution-event format.
Adapters convert provider-specific traces into it. `corpus/` is then pure data: scenario
YAML plus expectations, no Python at all. Invariants are typed predicates over schema v1
and live in the package.

**Storage guarantees are stated per backend.** `InMemoryStore` is single-process and
explicitly non-durable. `SqliteStore` runs in WAL mode with a `UNIQUE` constraint on the
idempotency key, so atomicity comes from the database rather than from an
application-level check-then-write, which races.

**The dependency graph is one-way and enforced.** `cli -> runner -> {invariants, report}
-> ledger`. `import-linter` contracts in `pyproject.toml` fail CI on a violation, so the
boundary is a build error rather than a code-review opinion.

## Consequences

- Adding an effect to the ledger core means adding a Protocol, not an import. This is
  friction by design.
- The schema becomes a public contract with its own version and deprecation policy.
  Breaking it is a major-version event, and contract tests pin v1 cassettes.
- The wheel stays single-licensed. The license boundary is a directory boundary, so it is
  checked mechanically rather than argued about: `scripts/check_licenses.py` requires a
  BUSL-1.1 declaration on *every* file under `src/ledgergate/`, not only the `.py` ones,
  because package data added later would otherwise enter the wheel unlabelled.
- Corpus resolution needs an explicit path, so the CLI must fail with a clear message
  when it is missing rather than silently scoring zero scenarios.
- Claims in the README and in interviews are bounded by what the gates enforce. Anything
  the gates do not prove does not get claimed.

## Alternatives rejected

- **Flat top-level packages.** Simpler imports, but unpublishable and collision-prone.
- **A corpus with no Python anywhere, including the checker.** Sounds cleaner, but the
  property that matters to an adopter is a stable data contract, not our language choice.
- **Documenting determinism as a convention.** Conventions are not gates. A single
  `datetime.now()` would pass review, pass mypy, pass the tests, and silently break
  replay only when two runs are diffed.
