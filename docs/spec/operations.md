<!--
SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
SPDX-License-Identifier: Apache-2.0
-->

# Operational readiness (M7.5): backup, rollover, limits, recovery, policies, rehearsal

**Status: design draft, not reviewed, not implemented.** This is the milestone the external
whole-repository review of 2026-09-06 named as the gap between "the code is right" and
"the code can be run": a rehearsed backup/restore and rollover procedure that preserves the
replay and approval-consumption history and the single-writable-lineage rule; documented
operational limits with alerts; load and crash-recovery testing; and supported-version,
upgrade and security-response policies. It comes before M8b because a network listener
widens the surface the review just hardened, and none of the above depends on one.

Everything here follows the project's standard: a claim is backed by a mechanism with a
stated invariant, exercised by a test or a rehearsed procedure, or it is withdrawn.

## 1. What the journal file is, operationally

One journal is one SQLite database in WAL mode (`journal.md`, *Concurrency*): the main file
`PATH`, a write-ahead log `PATH-wal`, and a shared-memory index `PATH-shm`. The guarantees the
journal makes hold for **one writable file lineage**: the approval consumption `UNIQUE`
(`approval_consumptions.approval_id`) and the replay `UNIQUE` (`signed_calls`) are
SQLite-local, so a second writable copy is a second lineage in which an approval or a signed
request can be spent again (`journal.md` *Known limit*, `principals.md` *does not claim*). Every
procedure below is therefore constrained by one rule, stated first:

> **Single writable lineage.** At any moment exactly one file is the journal that may be
> written. A backup is read-only until, and unless, it is *promoted*, and promotion retires
> the original. Two writable copies are two journals whose consumption histories can diverge,
> and nothing in the product reconciles them until M8c.

## 2. Backup and restore

### 2.1 Mechanism

`ledgergate journal backup PATH DEST` (new CLI) uses SQLite's online backup API over a
read-only connection (`sqlite3.Connection.backup`), which copies a consistent snapshot while a
`serve` process keeps writing, includes everything the WAL has committed at the snapshot, and
never copies the `-wal`/`-shm` files. The copy is written to a temporary name and renamed into
place, so `DEST` is either absent or complete. After the copy, the command:

1. opens `DEST` read-only, verifies `schema_version`, and asserts `probe()` (table set);
2. derives and verifies the trace of `DEST` (`derive` + `invariants.check`), refusing to
   report success on any `fail` (a backup that does not verify is not a backup);
3. prints the backup's **lineage record**: `journal_id`, the head sequence (max
   `journal_sequence`), the ledger head hash, the counts of `approval_consumptions` and
   `signed_calls`, and the SHA-256 of `DEST` — the facts a later restore is checked against;
4. marks `DEST` read-only on the filesystem (`0o444`) so it cannot be written by accident.

A file copy of `PATH` while `serve` is running is **not** a supported backup (the WAL may hold
committed rows the main file lacks); the command refuses to run against a `-wal` sibling that
is not its own, and the README says so.

### 2.2 Restore and promotion

`ledgergate journal restore SRC PATH` copies the backup to `PATH` (refusing an existing `PATH`
or an existing `PATH-wal`), clears the read-only bit, verifies as in 2.1, and prints the
lineage record. **Restore is promotion**: the operator states, by running it, that the original
lineage is retired. The command requires `--retire ORIGINAL` naming the retired file, which it
renames to `ORIGINAL.retired-<utc timestamp>` and marks read-only, or `--original-lost` when
the original is gone. What is lost by a restore is stated on the output: every invocation
after the backup's head sequence, every approval consumed and every signed call spent after
it — so an artefact consumed after the backup **can be presented again** against the restored
journal, and a signed request spent after it can be replayed. The remedy is operational: the
retired file, if it exists, is the record of what happened after the snapshot, and the
operator revokes any approver whose artefacts were consumed in the lost window (revocation is
a registry event the restored journal accepts) before reopening the journal to agents.

### 2.3 Rehearsal (a test, not a promise)

`tests/integration/test_backup_restore.py`: a journal under load (a writer thread applying,
approving and signing), `backup` taken mid-run and verified; then restore into a fresh path
with `--retire`, verification passes, the lineage record matches, the retired file is
read-only, and a re-presented artefact consumed *after* the snapshot is `approval_valid`
against the restored journal — the loss is asserted, not hidden. A second test: `backup`
refused against a foreign `-wal`; `restore` refused over an existing path.

## 3. Rollover

A journal has a stated capacity (`journal.md`: derived-trace events ≤ 5,000,000, checked
under the write lock; `mcp-runtime.md` *Segmentation*). Today the journal refuses at
capacity and `serve` reports it; there is no continuity procedure. Rollover is:

1. `ledgergate journal rollover OLD NEW` opens `OLD` (refusing if a `serve` holds the write lock:
   the operator stops the server first, stated), derives and verifies its trace, then creates
   `NEW` under the same chart, currencies, policy configuration and token key, with a
   **fresh `journal_id`** (a journal_id is never reused), and re-seeds `NEW`'s registries from
   `OLD`'s *live* principals and approvers (the live set at `OLD`'s head, attributed to the
   operator running the command, so the trace of `NEW` says who carried them over).
2. Writes into `NEW`'s definition a `predecessor` record: `OLD`'s `journal_id`, head sequence,
   ledger head hash and trace digest — so `NEW`'s trace names what came before it
   (additive definition column and trace-v2 field, schema 9).
3. Marks `OLD` read-only. Pending operations in `OLD` stay pending there (an operation is a
   fact of one journal); their artefacts cannot be presented to `NEW` (`journal_id` binding);
   the operator re-issues intents in `NEW` if they still want them. Stated, as in the
   earlier-schema procedure.

Opening balances are **not** carried into `NEW` as postings (that would be a command nobody
issued); `NEW` starts empty, and the pair (`OLD` trace, `NEW` trace) is the record. A
`ledgergate verify --chain OLD.json NEW.json` checks that `NEW.predecessor` names `OLD` and
that `OLD` verifies. What this does not claim: cross-journal replay protection (M8c).

## 4. Operational limits and alerts

Every limit the journal already enforces becomes an observable quantity, printed by
`ledgergate journal status PATH` (read-only) as JSON and, under `serve`, logged to stderr
once per N calls and at every threshold crossing:

| Quantity | Source | Alert threshold (default) |
| :-- | :-- | :-- |
| derived-event count vs capacity | the capacity formula (`journal.md`) | 80 % and 95 % of 5,000,000 |
| WAL size | `PATH-wal` bytes | 64 MiB (a reader pinning the WAL, or a checkpoint not happening) |
| main file size, free pages | `PRAGMA page_count`, `freelist_count` | informational |
| write-lock wait | time to acquire `BEGIN IMMEDIATE` | any wait over `BUSY_TIMEOUT_SECONDS / 2`; a `SQLITE_BUSY` refusal is logged as an unrecorded failure with its call id |
| integrity | `PRAGMA quick_check` on `status`; any `IntegrityError` under `serve` | any |
| registry | live principals/approvers, pending operations, stranded lines (`journal pending`) | any stranded operation |
| clock | `requested_at` monotonicity across the last N invocations | a step backwards |

`serve` gains `--checkpoint-every N` (default 1,000 invocations) running
`PRAGMA wal_checkpoint(TRUNCATE)` under the write lock, so the WAL does not grow without
bound under a long session; stated as the mechanism behind the WAL threshold. Alerts are
lines on stderr in one fixed grammar (`ledgergate alert <name> <value> <threshold>`), which
is what an operator's log shipper matches; there is no built-in pager integration, stated.

## 5. Load and crash-recovery testing

- **Load** (`tests/integration/test_load.py`, marked `slow`, run nightly): one `serve` process,
  four client threads over a shared stdio pipe? No — stdio has one client (`mcp-runtime.md`).
  Load is therefore a single client issuing 20,000 mixed calls (posts, reads, approvals,
  signed requests, replays) against one journal with the tokenizing admitter; assertions:
  every response is committed before it is served (the existing invariant), the derived trace
  verifies, throughput is *printed* (not asserted; the number is information), the WAL stays
  under the threshold with checkpoints on, and `status` at the end matches the trace.
- **Crash recovery** (`tests/integration/test_crash.py`): a subprocess `serve` is killed with
  `SIGKILL` at a random point in a scripted sequence, ten times; after each kill the journal
  opens, `PRAGMA integrity_check` is `ok`, the derived trace verifies, and the set of committed
  invocations is a prefix of the script (every served response is in the journal, every
  unserved call is absent or `invalid`-free) — the durability claim `journal.md` makes,
  exercised. Power loss is **not** simulated (a stated limit; SQLite's `synchronous = FULL`
  is the mechanism relied on, and the test proves process death only).
- **Concurrency** (extends the existing concurrent-writer tests): two `serve` processes on
  one journal with the busy timeout, interleaved approvals of one pending operation: exactly
  one `approval_valid`, the other `approval_already_used`, and one `signed_calls` spend per
  verified envelope.

## 6. Policies (documents, each short, each with a mechanism)

- **Supported versions** (`SUPPORT.md`): pre-1.0, only the latest alpha is supported; a
  schema bump is never a migration (the earlier-schema procedure, README). The table of
  schema ↔ first version is maintained in `journal.md` and tested (a meta test reads
  `SCHEMA_VERSION` and the table).
- **Upgrade policy** (`SUPPORT.md`): install the new version, open the journal; if refused by
  schema, follow the earlier-schema procedure; the trace and result schemas are versioned
  independently and additive within a major (`trace-v2.md`, `result/v1`).
- **Security response** (`SECURITY.md`, extended): private disclosure address, acknowledgement
  within 3 business days, fix or statement within 30 days for a confirmed high-severity issue,
  CVE requested through GitHub's advisory workflow, credit offered; the classes the project
  treats as in scope (journal integrity, double application, approval or replay bypass,
  attribution forgery, verifier false-pass) and out of scope (a client that opens the file
  directly; denial of service through the operator's own process). The Scorecard
  `Security-Policy` check reads this file; today it scores 9 for lacking disclosure timelines.

## 7. Owner-side prerequisites for the TestPyPI rehearsal

These are index-side and repository-side settings the workflow cannot create, stated as
blockers rather than assumed. Each has a check.

| Prerequisite | Where | Check before dispatch |
| :-- | :-- | :-- |
| TestPyPI trusted publisher: owner/repo `ledgergate`, workflow `release.yml`, environment `testpypi` | test.pypi.org → project `ledgergate` → Publishing (a pending publisher may be registered before the project exists) | `curl -s -o /dev/null -w '%{http_code}' https://test.pypi.org/pypi/ledgergate/json` is `404` (never published) and the publisher appears in the account's pending publishers |
| PyPI trusted publisher (same, environment `pypi`) | pypi.org | as above; **not** exercised by the rehearsal |
| GitHub environments `testpypi` and `pypi` exist; `pypi` has a required reviewer (the owner) so a tag push pauses for a human | repo → Settings → Environments | `gh api repos/:owner/:repo/environments --jq '.environments[].name'` lists both |
| Branch protection on `main`: PR required, the `gates`, `wheel`, `CodeQL`, `security` checks required, no force-push | Settings → Branches | `gh api repos/:owner/:repo/branches/main/protection` succeeds; Scorecard `Branch-Protection` rises from 0 |
| CodeQL check-failure threshold set (fail on high and above) | Settings → Code security | the CodeQL check on a PR is required (above) |
| `CHANGELOG.md` has a dated `## [0.1.0a1] - YYYY-MM-DD` section | repo | the `refuse` job asserts it |

## 8. The rehearsal itself (runbook)

Nothing below publishes to production. A rehearsal is `workflow_dispatch` on `main`; the
workflow's own guard refuses any other ref or event combination (meta-tested).

1. Confirm §7. Confirm `__version__` is `0.1.0a1` and unpublished on **both** indexes (the
   `refuse` job checks PyPI; check TestPyPI by hand, since a TestPyPI re-upload is refused
   and the rule is to bump the alpha number, never to skip).
2. `gh workflow run release.yml --ref main`. Record the run URL and `headSha`.
3. Watch the jobs in order: `refuse` (event+ref, version, changelog) → `gates` (the whole
   `ci.yml`, including the installed-wheel job) → `build` (locked backend, `--no-build-isolation`,
   attestations for wheel, sdist and corpus tarball) → `publish` (environment `testpypi`,
   trusted publishing, `attestations: true`) → `smoke` (fresh venv: dependencies from PyPI
   only, then `--no-deps` install of the package from TestPyPI, `ledgergate --version`,
   `ledgergate run --corpus corpus`, `report --conformance --require L2`). `release-assets`
   and `publish-release` are skipped by design on a dispatch.
4. Verify by hand what the workflow claims: `pip download --no-deps -i https://test.pypi.org/simple/ ledgergate==0.1.0a1`,
   then `gh attestation verify ledgergate-0.1.0a1-py3-none-any.whl --repo <owner>/ledgergate --signer-workflow <owner>/ledgergate/.github/workflows/release.yml`,
   and the same for the sdist; `pip show ledgergate` reports `License-Expression: BUSL-1.1`.
5. Record the outcome in `CHANGELOG.md` under the version's section ("rehearsed on TestPyPI:
   run …") and in the README roadmap's M7 row, replacing "release pipeline unrehearsed".
6. Failure modes and what they mean: `refuse` fails → a fact about the tree (fix the tree);
   `publish` fails with a trusted-publisher error → §7 not done; `publish` fails with "file
   already exists" → this alpha was already uploaded, bump to `0.1.0a2` (a rehearsal consumes
   an alpha number, stated); `smoke` fails → the published wheel does not run the corpus and
   **must not be tagged** until it does.

The first production tag (`v0.1.0a1` or the bumped alpha) is a separate, later decision after a
green rehearsal and the §7 `pypi` prerequisites; it is not part of this milestone's execution.

## 9. Order of work and review

1. This document reviewed (spec-review), amended, approved.
2. §7 owner actions (blocking for §8 only).
3. Implementation, in PRs, each with tests: `journal status` + alerts + checkpointing (§4);
   `journal backup`/`restore` + rehearsal test (§2); crash and load tests (§5); policies (§6);
   rollover with schema 9 (§3, its own design round since it touches the definition).
4. §8 rehearsal, recorded.
5. Then, and only then, the M8b design.

## What this document does not claim

- Cross-clone or cross-journal replay and consumption protection (M8c).
- Power-loss durability beyond what `synchronous = FULL` provides; only process death is
  tested.
- Pager or metrics-system integration; alerts are log lines in a fixed grammar.
- A migration of any kind between schemas.
