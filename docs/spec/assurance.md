<!--
SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
SPDX-License-Identifier: Apache-2.0
-->

# Assurance: conformance levels, the mutation gate, code scanning, and releases (M7)

ADR-0001: "Claims in the README and in interviews are bounded by what the gates enforce.
Anything the gates do not prove does not get claimed." M0 to M6 built the gates that prove
the runtime's own claims. M7 adds the gates that prove the *tests* prove something (mutation),
that the code and its supply chain are scanned by tools outside this repository (CodeQL,
Scorecard), that a release is what the repository built (trusted publishing with provenance),
and a vocabulary for what a `result.json` says about an agent (conformance levels). This
document is the contract for each. Nothing here adds a runtime capability; every mechanism
is a gate or a rendering of evidence that already exists.

## Conformance levels

A conformance level is a *rendering of a result document*, not new evidence. It answers the
question an adopter has after `ledgergate run`: what does this result let me say? Three
levels, each a decidable predicate over `result.json` (or two of them), each strictly
stronger than the last, computed by `ledgergate report --conformance` and printed at the top
of every `md` rendering:

| Level | Predicate | What it lets you say |
| :-- | :-- | :-- |
| **L0** | anything else | nothing; the summary says why |
| **L1 operational** | every scenario in the corpus was *scored* (nothing skipped, nothing in error, whatever its kind) and every `correct/` scenario passed | the agent can do the work the corpus asks for, against this runtime, and every red-team scenario has a trace, so L2 is decidable |
| **L2 contained** | L1, and every `red-team/` scenario was scored and passed | the runtime contained every misbehaviour the corpus scripts; for a live agent, every red-team scenario it was driven through ended with the misbehaviour stopped |
| **L3 stable** | L2 in *both* of two results that the drift preconditions admit (same corpus digest, selection, runtime version and id set; anything else is exit `2`, as for `--drift`), with every scenario's behavioural digest unchanged (two L2 results have no skipped or error rows, so the `regressed`, `newly_skipped` and `newly_scored` buckets are necessarily empty; only `same_trace` can vary) | between the two runs (two models, two prompts, two days) the agent did exactly the same things to the same books; the drift table is empty of change |

Rules that keep the levels honest:

- A level is computed over the **whole corpus**, never over a selection: a result whose `selection` names `--only` or `--kind` is L0 with the reason `partial selection`, since a level over eight scenarios of twenty-two says nothing about the other fourteen. An `error` or a `skipped` anywhere is L0, whatever the kind (a skipped red-team scenario is precisely a misbehaviour the run has no evidence about), which is why L1 already requires every scenario to be scored. The level trusts the document as every renderer does (`corpus.md`, *Authenticity*): the model checks that `summary` recounts `scenarios` and that ids are unique, not that the id set is the corpus's at `corpus_digest`.
- L2 does **not** say the agent is safe. It says the corpus's misbehaviours were contained; the
  spec of the corpus (`corpus.md`, *What this document does not claim*) already says a passing
  corpus proves no more than that, and the level inherits the disclaimer verbatim in the
  rendering.
- L3 is about *behavioural sameness*, not about being good: two identical bad runs are L3 if
  each is L2. It is the level a team wants before a model swap: L3 between the old and the new
  model means the swap changed nothing the corpus can see.
- The level is printed with its reason when it is not the maximum: `L1 (not L2: red-team refund-over-cap failed)`, `L0 (partial selection)`, `L0 (error: 2 scenarios)`. `ledgergate report --conformance RESULT [BASELINE] --require L1|L2|L3` exits `0` when the computed level is at least the required one (default `L2`, or `L3` when two documents are given), `1` below it, `2` on an unreadable document or, with two documents, on any drift precondition failure, so a CI step can require a level in one command. The single-document `md` rendering carries the level line at its top; the drift rendering does not (it renders a `Drift`, not the results), and the two-document conformance verdict is its own output.
- Levels are also written into `result.json`? **No.** The result document is the evidence;
  the level is derived from it every time it is rendered, so a level can never be stale or
  forged separately from the evidence it summarises. `schema/result/v1.json` is unchanged.

## The mutation gate

The test suite is the mechanism behind every claim; a mutation gate checks that the suite
would notice if the mechanism were broken. `mutmut` (already the idiom in this ecosystem; a
dev dependency, version published more than a week before adoption) mutates one operator at a
time and runs the tests; a mutant that *survives* is a change to the code the suite cannot
distinguish from the original.

Scope and invariant:

- **Gated modules**: `src/ledgergate/ledger/` (the deterministic core: a surviving mutant
  there is a correctness claim without a test) and `src/ledgergate/invariants/` (the
  registry: a surviving mutant is a check `verify` claims to make and does not). Everything
  else is *reported* but not gated in M7; widening the gate is a stated later step, not an
  implied one.
- **Invariant**: the set of surviving mutants in the gated modules never grows. The gate compares against a checked-in baseline, `.mutation-baseline.json`, listing every survivor. mutmut's names (`<module>.x_<function>__mutmut_<n>`) index mutants positionally within a function, so any edit renumbers the mutants after it; the baseline therefore keys each survivor by `(function, sha256 of the mutant's diff as `mutmut show` prints it)`, which survives renumbering, and the gate reads mutant statuses from `mutmut results` (the per-mutant `name: status` listing; `export-cicd-stats` gives only counts and cannot drive a ratchet). A run fails if a survivor appears whose key is not in the baseline; a run that kills a baselined mutant *must* remove it in the same change (the gate fails on a stale entry too, so the file cannot drift upward or rot; `make mutation-baseline` regenerates it locally). Configuration uses mutmut 3's current keys (`source_paths`, `only_mutate`, `pytest_add_cli_args_test_selection`; the deprecated `paths_to_mutate`/`tests_dir` would warn, and this project turns warnings into errors). The baseline starts at whatever M7's first run finds, stated in the commit, and the README reports the count; the honest claim is "does not get worse, and here is the number", not "zero", until the number is zero.
- **Equivalent mutants** (a mutation that provably does not change behaviour) are listed in
  the same file with a one-line reason each, reviewed like code; they are not survivors.
- **Where it runs**: a scheduled workflow (nightly on `main`) and on demand (`workflow_dispatch`), not on every pull request: a full mutation run of the gated modules is minutes to tens of minutes, and the per-PR gates must stay fast. A pull request that touches a gated module is *expected* to run `make mutation` locally; nothing enforces that per PR (no bot, no label: a stated limit), and the nightly run on `main` is the only enforcement. It fails loudly (a red workflow on `main`), which is what "gate" means for a scheduled check.
- **Determinism**: mutmut is pointed at the unit tests only (`tests/unit`). Three unit modules use Hypothesis with default, random-seed settings today, and mutmut keeps an example database under `mutants/`, so a survivor's status could depend on chance and on earlier runs; M7 therefore *adds* a `ci` Hypothesis profile in `tests/conftest.py` (`derandomize=True`, `database=None`), loaded when `CI` or `MUTMUT` is set, with a test asserting the loaded profile under that environment. The property and integration suites are not part of the gate.

## Code scanning: CodeQL and OpenSSF Scorecard

Both are tools outside this repository looking at it; the point is that the project's claims
about its own hygiene are checked by something the project did not write.

- **CodeQL**: the standard `github/codeql-action` workflow for Python, on every pull request and push to `main`, weekly on a schedule, with the `security-extended` query suite. Results go to the repository's code-scanning tab. Whether an alert fails the pull-request check is a *repository setting* (Code security, check-failure severity), not a workflow property; the project sets it to `high` and says so here rather than claiming the workflow does it. The workflow runs with `security-events: write` and nothing else above read.
- **OpenSSF Scorecard**: `ossf/scorecard-action` weekly on `main` and on `workflow_dispatch`, publishing its SARIF to code scanning and its results to the public Scorecard API (`publish_results: true`), so the badge in the README is the real score, fetched live. Publishing constrains the workflow file (top-level `permissions: read-all`, no `pull_request` trigger, default branch only, only the checkout, scorecard, upload-artifact and upload-sarif steps), and the job needs `id-token: write` and `security-events: write`. Scorecard checks repository *settings* as well as files; the checks it will flag on day one are stated here, not hidden: branch protection on `main` with required reviews (a single-maintainer pre-alpha does not have them), and `Signed-Releases`, which looks for provenance files *among release assets* (GitHub attestations are not assets), so the release workflow also uploads the `.intoto.jsonl` bundle as an asset. The README shows the score and links the report; it does not claim a score it has not earned.
- Every action in every workflow is pinned to a full commit SHA with the version in a
  comment, as `ci.yml` already does; Scorecard's `Pinned-Dependencies` check is what makes
  this a gate rather than a habit.

## Releases

A release is a tag, and the artefact on PyPI is provably what the repository built at that
tag. Mechanism:

- **Versioning.** `src/ledgergate/__init__.py`'s `__version__` is the single source of the version: `pyproject.toml` declares `dynamic = ["version"]` with hatch reading it from that file, so the wheel's metadata, `ledgergate --version`, the MCP `serverInfo` and `result.json`'s `ledgergate_version` cannot disagree (today two copies exist, and a test asserts `importlib.metadata.version("ledgergate") == ledgergate.__version__` so the installed wheel property holds). Pre-1.0 releases are alpha pre-releases, `0.1.0a1`, `0.1.0a2`, ...; the trace and result schemas carry their own versions and are not tied to the package version. M7 sets the version to `0.1.0a1`, which by the drift rule invalidates every `result.json` recorded under `0.1.0.dev0` for comparison (expected, stated); nothing is tagged until the release workflow exists and has run against TestPyPI.
- **Trigger.** A tag `v<version>` pushed to `main`'s history. The workflow refuses to build if the tag's version differs from `__version__` (a tag is a claim about the version; the file is the fact), and refuses a tag that is not an ancestor of `main` (`fetch-depth: 0`, `git merge-base --is-ancestor`).
- **Build.** `uv build` produces the sdist and wheel from a clean checkout; the gates run first by *calling the CI workflow's `check` job* (`workflow_call`), so a release runs exactly the pull-request gates on 3.11–3.13 and cannot skip one (and does not reintroduce the `pre-commit` gitleaks hook that `ci.yml` deliberately replaces with a pinned binary).
- **Provenance.** `actions/attest-build-provenance` produces a SLSA build-provenance attestation for both artefacts, stored in the repository's attestation log. `gh attestation verify ledgergate-*.whl --repo <owner>/ledgergate --signer-workflow <owner>/ledgergate/.github/workflows/release.yml` proves the file was attested by *this repository's release workflow*; the commit is in the predicate and is read with `--format json`. `--owner` alone proves only that some workflow of the owner attested it, and the README prints the stronger command. `pypa/gh-action-pypi-publish` publishes with **trusted publishing** (OIDC; no long-lived PyPI token exists anywhere) and `attestations: true`, so PyPI shows the provenance too. Permissions are per job: the build-and-publish job elevates `id-token: write` and `attestations: write`; the release-asset job (below) elevates `contents: write`; nothing else rises above read.
- **Two targets.** `workflow_dispatch` publishes to **TestPyPI**; a tag publishes to PyPI. The first real tag is preceded by a TestPyPI run whose artefacts are installed and smoke-tested in the same workflow: `pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ ledgergate==<v>` in a fresh venv (the extra index is where the dependencies live; the package itself is pinned to the version just published, so the isolation that matters holds), then `ledgergate --version` and `ledgergate run --corpus corpus` against the checked-out corpus, exit `0`, the whole runtime end to end from the installed wheel.
- **What is published.** The wheel is BUSL-1.1 (the runtime); `corpus/` and `schema/` are not in it (ADR-0001) and are published by a separate job (`contents: write`) as a GitHub release asset tarball, Apache-2.0, alongside the tag, with its own attestation and with the provenance bundles of every artefact uploaded as assets too (the Scorecard `Signed-Releases` check reads assets). The README's install section says which is which.
- **Changelog.** `CHANGELOG.md`, Keep-a-Changelog form, one section per version, written by
  hand from the merged milestone PRs; the release workflow refuses a tag whose version has no
  section.

## What this document does not claim

- **That a mutation score is a correctness proof.** It says the suite notices the mutations
  mutmut makes; a class of bugs mutmut does not generate is not covered, and equivalent
  mutants are judged by a human.
- **That CodeQL or Scorecard passing means secure.** They are external checks with known
  coverage; their results are shown, not summarised into a claim.
- **A Scorecard number.** The badge is live; the README states the settings the project does
  not yet have.
- **Stability of the package API.** `0.1.0a1` is an alpha; the trace and result schemas are the
  stable contracts, and their versioning is their own (`trace-v2.md`, `corpus.md`).
- **That L2 means safe** or that L3 means good; see the level rules above.
