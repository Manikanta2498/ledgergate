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

The rule needs a mechanism inside the journal, because nothing outside it can hold: SQLite's
write lock is per transaction (`BEGIN IMMEDIATE`, `journal.md` *Concurrency*), so between
invocations a running `serve` holds nothing and "is a server writing?" is undecidable; and a
filesystem read-only bit does not reach a descriptor a process already holds open (POSIX checks
permissions at `open`), so a stale `serve` would keep appending. The mechanism is the **seal**
(schema 9): an append-only `lineage` table whose rows are `sealed` (with `successor_journal_id`
or `restored_to`, and the head sequence at sealing) or `restored_from` (below). The binding
check every transaction already re-asserts (`journal.md` *Tables*, `definition`) refuses any
write once a `sealed` row exists, with its own cause (`JournalSealed`), so a stale server's
*next transaction fails inside the journal*, and the seal is written in the same `BEGIN
IMMEDIATE` that reads the head it records, so the recorded head is the head. A seal costs one
event under the capacity formula; a journal exactly at capacity accepts it (the one stated
exemption: sealing is how a full journal is retired).

## 2. Backup and restore

### 2.1 Mechanism

`ledgergate journal backup PATH DEST` (new CLI) uses SQLite's online backup API over a
read-only connection (`sqlite3.Connection.backup(..., pages=-1)`, a single step, so the copy is
one read transaction's view; a multi-step backup restarts under a foreign writer), which copies
a consistent snapshot while a `serve` process keeps writing, includes everything the WAL has
committed at the snapshot, and never copies the `-wal`/`-shm` files. The copy is written to a temporary name and renamed into
place, so `DEST` is either absent or complete. After the copy, the command:

1. opens `DEST` read-only, verifies `schema_version`, and asserts `probe()` (table set);
2. derives and verifies the trace of `DEST` (`derive` + `invariants.check`), refusing to
   report success on any `fail` (a backup that does not verify is not a backup);
3. prints the backup's **lineage record**: `journal_id`, the head sequence (max
   `journal_sequence`), the ledger head hash, the counts of `approval_consumptions` and
   `signed_calls`, and the SHA-256 of `DEST` — the facts a later restore is checked against;
4. marks `DEST` read-only on the filesystem (`0o444`) so it cannot be written by accident.

A file copy of `PATH` while `serve` is running is **not** a supported backup (the WAL may hold
committed rows the main file lacks), and the README says so. Nothing identifies which main file
a WAL belongs to (a WAL header carries salts and checksums, not its owner), so no tool can
detect a "foreign" WAL; the claim is not made.

### 2.2 Restore and promotion

`ledgergate journal restore SRC PATH --retire ORIGINAL | --original-lost` promotes a backup and
retires the lineage it came from, in this order, each step a mechanism:

1. **Seal the original** (when it exists): open `ORIGINAL` read-write, `BEGIN IMMEDIATE`, read
   the head, append `lineage(sealed, restored_to=<SRC lineage record>, at_sequence=head)`,
   commit. From this commit every writer of `ORIGINAL`, a running `serve` included, is refused
   at its next transaction (`JournalSealed`). If the seal cannot be written (the file is
   already sealed, or is not a journal), the command stops here and says so.
2. **Checkpoint and rename.** In autocommit, `PRAGMA wal_checkpoint(TRUNCATE)`; the command
   asserts the returned `busy` flag is `0` and `ORIGINAL-wal` is absent or empty (a reader
   pinning the WAL makes the checkpoint incomplete, and the command refuses rather than rename
   a main file away from a WAL that still holds its rows), then renames `ORIGINAL` to
   `ORIGINAL.retired-<utc>` and marks it `0o444`. The retired file is then, and only then, the
   complete record of what happened after the snapshot. When `PATH == ORIGINAL` this step runs
   before the copy, so the copy never meets an existing `PATH-wal`.
3. **Copy and record.** Copy `SRC` to `PATH` (refusing an existing `PATH` or `PATH-wal`), clear
   the read-only bit, verify as in 2.1, and append to the *restored* journal
   `lineage(restored_from, backup_sha256, head_sequence, head_hash, retired=<path or lost>)`, so
   the restored journal's own trace says it was restored and from what: a later reader of the
   trace is not shown a journal that never lost anything. Both lineage rows are trace-v2 events
   (`lineage_change`, additive), and `verify` checks that a `restored_from` head hash equals the
   ledger head at that sequence.

What is lost by a restore is thereby recorded, not only printed: every invocation after the
backup's head sequence, every approval consumed and every signed call spent after it. An
artefact for an operation that was *pending at the snapshot* and consumed after it is
`approval_valid` again against the restored journal (an operation created after the snapshot is
not there to approve: its artefact is `approval_scope_mismatch`); a signed request spent after
the snapshot is replayable against the restored journal until its `expires_at` (at most a day,
`principals.md`). The remedy is operational and stated: the retired file lists the approvers
and signed principals active in the lost window; the operator revokes those approvers and, for
the day the bound allows, those signed principals, before reopening the journal to agents.

### 2.3 Rehearsal (a test, not a promise)

`tests/integration/test_backup_restore.py`: a journal under load (a writer thread applying,
approving and signing), with one operation *pending* when `backup` is taken and verified; the
writer then consumes that operation's artefact; restore into a fresh path with `--retire`:
verification passes, the lineage record matches, the original is sealed (its writer's next
call is `JournalSealed`), checkpointed, renamed and read-only, the restored journal carries its
`restored_from` row, and the artefact consumed after the snapshot is `approval_valid` again
against the restored journal — the loss is asserted, not hidden. Further tests: `restore`
refused over an existing path; `restore` refused when the checkpoint reports `busy`
(a held read transaction in the test); a sealed journal refuses every write and still derives.

## 3. Rollover

A journal has a stated capacity (`journal.md`: derived-trace events ≤ 5,000,000, checked
under the write lock; `mcp-runtime.md` *Segmentation*). Today the journal refuses at
capacity and `serve` reports it; there is no continuity procedure. Rollover applies **from
schema 9 onward**: the schema-9 bump itself sends every schema-8 journal through the
earlier-schema procedure (README), since a schema-9 build cannot open one and cannot write a
predecessor record into a file it may not open. From schema 9:

1. `ledgergate journal rollover OLD NEW --policy … --token-key-file …` opens `OLD` under the
   same components `serve` would use (a non-declarative policy set exists only as a digest in
   the definition, so the set comes from the flags and its digest must match `OLD`'s, as at
   `open`; the token key likewise), derives and verifies its trace, and **seals it** in one
   `BEGIN IMMEDIATE` with `successor_journal_id` = `NEW`'s fresh id and the head sequence. A
   running `serve` on `OLD` is refused at its next transaction; nothing needs to be stopped
   first, though the operator will want to.
2. Creates `NEW` under the same chart, currencies, policy configuration and token key, with a
   fresh `journal_id` (never reused), and re-seeds `NEW`'s registries from `OLD`'s *live*
   principals and approvers at the sealed head, each `add` attributed (`by`) to the operator
   running the command, so `NEW`'s trace says who carried them over and from where.
3. Writes `NEW`'s `lineage(predecessor, journal_id=OLD's, head_sequence, head_hash,
   trace_digest)` row. `ledgergate verify --chain OLD.json NEW.json` checks that `NEW`'s
   predecessor row names `OLD`'s id and head, that `OLD` verifies, and that `OLD`'s last
   lineage row is the seal naming `NEW`.

Pending operations in `OLD` stay pending there (an operation is a fact of one journal); their
artefacts cannot be presented to `NEW` (`journal_id` binding); the operator re-issues intents
in `NEW`. Opening balances are **not** carried into `NEW` as postings (that would be a command
nobody issued); `NEW` starts empty, and the pair (`OLD` trace, `NEW` trace) is the record. What
this does not claim: cross-journal replay protection (M8c).

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
`PRAGMA wal_checkpoint(TRUNCATE)` on its own connection **in autocommit, between
invocations** (a checkpoint inside `BEGIN IMMEDIATE` is refused by SQLite with
`SQLITE_LOCKED`), logging the returned `(busy, log, checkpointed)` triple; the WAL alert fires
when `busy = 1` on consecutive checkpoints (a reader pinning the WAL) or the WAL exceeds the
size threshold. The clock alert is per process (two servers on two hosts have two clocks). Alerts are
lines on stderr in one fixed grammar (`ledgergate alert <name> <value> <threshold>`), which
is what an operator's log shipper matches; there is no built-in pager integration, stated.

## 5. Load and crash-recovery testing

- **Load** (`tests/integration/test_load.py`, marked `slow`, run nightly): one `serve` process,
  four client threads over a shared stdio pipe? No — stdio has one client (`mcp-runtime.md`).
  Load is therefore a single client issuing 20,000 mixed calls (posts, reads, approvals,
  signed requests, replays) against one journal with the tokenizing admitter; assertions:
  the derived trace verifies, throughput is *printed* (not asserted; the number is information), the WAL stays
  under the threshold with checkpoints on, and `status` at the end matches the trace.
- **Crash recovery** (`tests/integration/test_crash.py`): a subprocess `serve` is killed with
  `SIGKILL` at a random point in a scripted sequence, ten times; the harness issues calls
  strictly one in flight; after each kill the journal opens, `PRAGMA integrity_check` is `ok`,
  the derived trace verifies, every served response's `call_id` is present as a committed
  invocation, and the committed invocations are exactly the served ones plus at most the one
  call in flight at the kill (nothing beyond it) — the durability claim `journal.md` makes,
  exercised. Power loss is **not** simulated (a stated limit; SQLite's `synchronous = FULL`
  is the mechanism relied on, and the test proves process death only).
- **Concurrency** (extends the existing concurrent-writer tests): two `serve` processes on
  one journal with the busy timeout, interleaved approvals of one pending operation: exactly
  one `approval_valid`, the other `approval_already_used`, and one `signed_calls` spend per
  verified envelope.

## 6. Policies (documents, each short, each with a mechanism)

- **Supported versions** (`SUPPORT.md`): pre-1.0, only the latest alpha is supported; a
  schema bump is never a migration (the earlier-schema procedure, README). A table of
  schema ↔ first version is *to be added* to `journal.md` and tested (a meta test reads
  `SCHEMA_VERSION` and the table); it does not exist yet.
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
| Branch protection on `main`: PR required, the check contexts `gates (py3.11)`, `gates (py3.12)`, `gates (py3.13)`, `installed wheel runs the corpus`, `analyze (python)`, `security` required, no force-push | Settings → Branches | `gh api repos/:owner/:repo/branches/main/protection --jq '.required_status_checks.contexts'` lists exactly those; Scorecard `Branch-Protection` rises from 0 |
| CodeQL check-failure threshold set (fail on high and above) | Settings → Code security | the CodeQL check on a PR is required (above) |
| `CHANGELOG.md` has a dated `## [0.1.0a1] - YYYY-MM-DD` section | repo | the `refuse` job asserts it |

## 8. The rehearsal itself (runbook)

Nothing below publishes to production. A rehearsal is `workflow_dispatch` on `main`; the
workflow's own guard refuses any other ref or event combination (meta-tested).

1. Confirm §7. Confirm `__version__` is `0.1.0a1` and unpublished on **both** indexes, by
   hand: on a dispatch no job checks an index (the PyPI presence check lives in
   `release-assets`, which a dispatch skips), so the only guard is TestPyPI's own refusal of a
   re-upload at `publish`, and the rule is then to bump the alpha number, never to skip.
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
   crash and load tests (§5); policies (§6); then schema 9 in one PR: the `lineage` table,
   `JournalSealed`, `journal backup`/`restore` with the seal (§2) and `rollover` (§3), with the
   trace-v2 `lineage_change` event and the `--chain` check, its own design round since it
   changes the schema and the trace.
4. §8 rehearsal, recorded.
5. Then, and only then, the M8b design.

## What this document does not claim

- Cross-clone or cross-journal replay and consumption protection (M8c).
- Power-loss durability beyond what `synchronous = FULL` provides; only process death is
  tested.
- Pager or metrics-system integration; alerts are log lines in a fixed grammar.
- A migration of any kind between schemas.
