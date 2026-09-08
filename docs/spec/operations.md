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
write lock is per transaction (`BEGIN IMMEDIATE`, `journal.md` *Concurrency*), so a running
`serve` may be writing at any moment; and a filesystem read-only bit does not reach a
descriptor a process already holds open (POSIX checks permissions at `open`), so a stale `serve`
would keep appending. Two mechanisms, one against writes and one against a live descriptor:

- **The seal** (schema 9): an append-only `lineage` table whose rows are `sealed` (with
  `successor_journal_id` or `restored_to`, and the head sequence at sealing), `restored_from` or
  `predecessor` (below), every row carrying `by` (a live transport principal, trigger-checked as
  registry rows are) and `at`: sealing halts every agent on the journal and is attributed like
  any other write. Enforcement is at the database, not in one code path: a `BEFORE INSERT`
  trigger on every table refuses any row once a `sealed` row exists (and a second `sealed` row),
  so a non-ledgergate writer holding an old descriptor is refused too; and the journal checks
  `SELECT 1 FROM lineage WHERE kind = 'sealed'` inside every transaction, immediately after
  `BEGIN IMMEDIATE` and before any insert, on every path including registry-only opens, to refuse
  with its own cause (`JournalSealed`) rather than an `IntegrityError`. The seal is written in
  the same `BEGIN IMMEDIATE` that reads the head it records, so the recorded head is the head.
  Schema 9 adds `+ count(lineage)` to the capacity formula (`journal.md`), a lineage row
  costing 1 like a registry row, so every lineage event is counted. **The seal itself bypasses
  the formula** (rollover exists for the full journal, and the formula can sit exactly at the
  bound), and relies on this invariant instead: the formula charges 9 per invocation while an
  invocation derives at most 8 events, and every other counted row derives exactly one, so the
  actual event count is at most `total - invocations`, and `actual + 1 <= 5,000,000` whenever there is at
  least one invocation; with none, every counted row derives exactly one event, so the seal
  checks the actual count against the bound directly and is refused only if the journal holds
  5,000,000 events without a single invocation. The check order inside every transaction is
  fixed once, here and in `journal.md`/`mcp-runtime.md` at implementation: binding, then seal,
  then capacity, so a sealed-and-misbound or sealed-and-full journal reports one stated cause.
- **The live-descriptor signal.** A WAL-mode connection holds the `-shm` file for its lifetime,
  and SQLite removes `PATH-wal` and `PATH-shm` when the last connection closes *if that
  connection is read-write* (a read-only connection closing last leaves them, and a connection
  that has opened but run no statement is not counted). So the command closes its read-only
  handles first, keeps one read-write connection, checkpoints with `busy = 0`, closes it, and
  then `ORIGINAL-wal` or `ORIGINAL-shm` still existing is a decidable fact: another connection
  is open. Between that check and the rename there is a window in which a fresh open (a
  supervisor restarting `serve`) recreates the files under the path; the seal makes the
  consequence a refused writer rather than stray frames, which is the honest claim. Every procedure that renames or replaces a main
  file refuses in that case and never deletes those files (SQLite binds a WAL to its main file
  by *path*; a deleted-and-recreated pair under one path would be shared between the stale
  connection's inode and the new journal, and frames the stale connection writes would be
  applied by page number to the new file).

## 2. Backup and restore

### 2.1 Mechanism

`ledgergate journal backup PATH DEST` (new CLI) uses SQLite's online backup API over a
read-only connection (`sqlite3.Connection.backup(..., pages=-1)`, a single step, so the copy is
one read transaction's view; a multi-step backup restarts under a foreign writer), which copies
a consistent snapshot while a `serve` process keeps writing, includes everything the WAL has
committed at the snapshot, and never copies the `-wal`/`-shm` files. The copy is written to a temporary name and renamed into
place, so `DEST` is either absent or complete. After the copy, the command:

1. opens `DEST` with `?immutable=1` (nothing else can have it open: it was just written, and
   SQLite then neither needs nor creates `-wal`/`-shm`; a plain read-only open of a WAL-header
   file with no `-shm` is not portable across SQLite builds, 3.51 refuses it where 3.53 does
   not, so the minimum SQLite version is stated in `pyproject.toml`'s metadata and pinned by a
   meta test), verifies `schema_version`, and asserts `probe()` (table set);
2. derives and verifies the trace of `DEST` (`derive` + `invariants.check`), refusing to
   report success on any `fail` (a backup that does not verify is not a backup);
3. writes the backup's **lineage record** to `DEST.lineage.json` and prints its path:
   `journal_id`, the head sequence (max `journal_sequence`), the ledger head hash, the counts of
   `approval_consumptions` and `signed_calls`, and the SHA-256 of the main file `DEST` — the
   facts a later restore is checked against;
4. marks `DEST` read-only on the filesystem (`0o444`) so it cannot be written by accident.

A file copy of `PATH` while `serve` is running is **not** a supported backup (the WAL may hold
committed rows the main file lacks); the README will say so when this ships. Nothing identifies which main file
a WAL belongs to (a WAL header carries salts and checksums, not its owner), so no tool can
detect a "foreign" WAL; the claim is not made.

### 2.2 Restore and promotion

`ledgergate journal restore SRC PATH --lineage-record FILE (--retire ORIGINAL | --original-lost)`
promotes a backup and retires the lineage it came from. **Invariant: no irreversible step
precedes every check that could refuse the restore.** Resume recognition comes first: the
command reads the state of `SRC`, `ORIGINAL`, `PATH` and any `ORIGINAL.retired-*` (identified
by a `sealed` row whose `restored_to` equals the lineage record, never by `journal_id`, which
every backup shares) and enters the procedure at the step that state names; the checks of step
0 that a resumed state has already passed irreversibly (the SHA of a `SRC` that is now sealed;
the absence of a `PATH` that now exists with `SRC`'s id) are suspended for that state and every
other check runs. In order for a fresh run:

0. **Check everything, change nothing.** Open `SRC` with `?immutable=1` (it is `0o444` and
   nothing writes it); verify `schema_version` and
   `probe()`; derive and `invariants.check` (any `fail` refuses); recompute its SHA-256 and
   compare every field of the lineage record written at backup time (`--lineage-record`, the
   file `backup` wrote; the comparison is the mechanism, a substituted backup with a valid
   internal structure is refused); when `--retire ORIGINAL`, open `ORIGINAL` read-only and
   require its `journal_id` to equal `SRC`'s (a retire pointed at another journal seals an
   unrelated lineage); when `PATH != ORIGINAL`, refuse an existing `PATH`, `PATH-wal` or
   `PATH-shm` (when `PATH == ORIGINAL`, the in-place case, `PATH` is the original and is
   retired in step 2). And check that the `restored_from` row of step 3 **can be inserted**,
   since that insert is the last step and must not be the one that refuses: the operator's
   principal is live in `SRC`'s registry (the row's `by` is trigger-checked against *SRC's*
   registry, which may predate the operator's add), `SRC` carries no `sealed` row (a backup of a
   rolled-over or already-promoted journal is not promotable), and `SRC` admits one more event
   under the capacity formula. Only when every check passes does anything below run.
1. **Seal the original** (when it exists): `BEGIN IMMEDIATE`, read the head, append
   `lineage(sealed, restored_to=<SRC record>, at_sequence=head, by, at)`, commit. From this
   commit every writer of `ORIGINAL` is refused (`JournalSealed`; the trigger behind it). An
   `ORIGINAL` that is already sealed *with this same `restored_to` record* is a resumed restore
   and continues; sealed otherwise, it refuses.
2. **Checkpoint, then decide whether the descriptor is free, then rename.** In autocommit,
   `PRAGMA wal_checkpoint(TRUNCATE)`; require `busy = 0`. Close the command's connection. If
   `ORIGINAL-wal` or `ORIGINAL-shm` still exists, another connection holds the file (§1): the
   command **refuses here and deletes nothing**, printing that the sealed original must be
   released (the stale `serve` is already refused on its next write and may be stopped at
   leisure; the restore is re-run, resuming at step 1). Otherwise rename `ORIGINAL` to
   `ORIGINAL.retired-<utc>` and mark it `0o444`. The retired file is then the complete record
   of what happened after the snapshot: its own head, checkpointed into it.
3. **Copy, record, then link.** Copy `SRC` to a temporary name beside `PATH` and, on the
   temporary file, append the restored journal's lineage rows *before it becomes `PATH`*, so no
   moment exists in which `PATH` is a valid journal without them (a supervisor-started `serve`
   would otherwise honour window artefacts against a journal that does not yet know the
   window): `lineage(restored_from, backup_sha256, backup_head_sequence, backup_head_hash,
   original_head_sequence, original_head_hash, retired=<'retired' | 'lost'>,
   retired_sha256, by, at)`, and, when the original was retired, one `lineage(lost_consumption,
   approval_id)` row per `approval_consumptions` row of the retired file with
   `journal_sequence > backup_head_sequence` and one `lineage(lost_spend, principal, call_id)`
   row per `signed_calls` row likewise — the exact set of what was spent in the window, read
   from the file that recorded it, no clock involved. Then `PRAGMA wal_checkpoint(TRUNCATE)`,
   close, assert `tmp-wal`/`tmp-shm` absent (a WAL is bound by path; frames left beside `tmp`
   would be lost at the link), and **link** into place (`os.link(tmp, PATH)`, which fails with
   `EEXIST` if `PATH` appeared meanwhile, then unlink `tmp`): two restores racing past step 1
   cannot both create `PATH`, and a shared `PATH-wal` between two inodes, the hazard of §1,
   cannot arise. Every lineage row is a trace-v2 event (`lineage_change`, additive); `verify`
   checks that a `restored_from`'s backup head hash equals the ledger head at that sequence.
   Finally **seal `SRC`** itself (clearing its read-only bit for the write, `sealed,
   restored_to=<backup_sha256, restore_at>`, the `restored_from` row's own identity, which
   unlike `PATH`'s bytes never changes, then restoring the bit), so a backup is promotable once:
   a second promotion from the same file would be a second writable lineage, and the seal is
   what refuses it. The `retired` field is a fixed marker and a SHA, never a path (a path is
   operator free text and would enter the trace).

**Resume states**, each recognisable, each continuing at the named step, **earliest step
first** (so in the in-place case a sealed `ORIGINAL` that is also `PATH` is step 2, not a
`PATH` awaiting its row; `PATH`-present rules apply only once `ORIGINAL` is renamed or
absent): `ORIGINAL` sealed with this `restored_to` → step 2; `ORIGINAL` absent and a retired
file whose seal names this record → step 3, with that file as the source of the original head
and the lost rows; `PATH` present with `SRC`'s id (its rows were written before the link, so it
has them) and `SRC` unsealed → seal `SRC`; `SRC` sealed with this `restored_from` identity →
done. Two restores racing past step 1 both see "sealed with this record" and continue; only
one can link `PATH` (step 3), and the other refuses there. The lineage record
is an unsigned JSON file: the comparison refuses a substituted backup presented *with the
original record*; a forger who replaces both is outside what the record can detect, stated.

What is lost by a restore is thereby recorded: every invocation between the backup head and
the original head, every approval consumed and every signed call spent in that window. The
exposure is stated at its true width: **every artefact issued in the window is live against the
restored journal**, not only those for operations pending at the snapshot — an artefact for an
operation the restored journal does not know is `approval_not_applicable` on presentation, but
the honest client then re-issues the intent, which recreates the operation with the same
fingerprint, tokenized key and `journal_id`, and the old artefact then passes every check —
*unless the journal knows the window*, which is the mechanism below. A signed request spent in
the window is replayable until its `expires_at` (at most a day, `principals.md`).

**The window is closed by a mechanism, not by revocation.** Revoking a named approver is
permanent (`principals.md`: a revoked name is never re-added, and a policy line naming it is
stranded, its journal then needing a new definition), so "revoke everyone live in the window"
would leave every approval-gated operation of a restored journal permanently unapprovable and
§3's rollover refused; that is not a remedy. The mechanism is the lineage rows of step 3, which
the journal consults exactly where the lost `UNIQUE`s would have spoken:

- **Approvals** (`--retire`): check 4 consults `lost_consumption` rows alongside
  `approval_consumptions`; an artefact whose `approval_id` was consumed in the window is
  `approval_already_used`, the existing verdict for exactly this fact, whenever it was issued
  (an artefact issued *before* the backup head and consumed inside the window is the case an
  `issued_at` window would miss, since artefact validity is unbounded). An artefact issued in
  the window and *never* presented stays honourable: the approver approved that fingerprint,
  and honouring it once is the product's promise.
- **Signed requests** (`--retire`): the replay check consults `lost_spend` rows alongside
  `signed_calls`; a pair spent in the window is `replayed_call`. No quarantine is needed, since
  the set is exact.
- **`--original-lost`**: there is no file to read the sets from, so the journal cannot know
  what was consumed, and it says so with a clock-bounded refusal instead: the `restored_from`
  row records `restore_at` (the restore command's clock, stated); an artefact whose `issued_at`
  precedes `restore_at` is refused with a new closed-vocabulary value, `approval_lost_window`
  (on `approvals.check_result`, `decisions.approval_verdict`, v2 `Verdict` and `check_result`;
  `approval_invalid` cannot be reused, since the signature verifies and the model requires
  `verified = false` for that result), and every envelope with `expires_at <= restore_at +
  86,400 s` is refused as `request_in_lost_window` (envelope validity is bounded, so this covers
  every envelope that could have been spent before the restore; for the first minute after a
  restore no envelope can satisfy it, given `sign`'s 86,340 s cap, and for some hours only
  near-maximal expiries can, stated). The clock assumption is stated: an approver clock ahead
  of the restore command's by more than its skew lets a pre-restore artefact through, and the
  operator re-issues approvals for pending operations after a lost-original restore.

The trace invariant `attributions_are_registered` gains the corresponding clauses (no
`approval_valid` on an `approval_id` a `lost_consumption` row names; no `approval_valid` on an
artefact issued before a `restore_at` without a retired original). The single-writable-lineage
rule under `--original-lost` rests on the operator, stated under *does not claim*.

### 2.3 Rehearsal (a test, not a promise)

`tests/integration/test_backup_restore.py`: a journal under load (a writer thread applying,
approving and signing), with one operation *pending* when `backup` is taken and verified, and one
opened and approved entirely after it; restore into a fresh path with `--retire` and the
lineage record: verification passes, the original is sealed (its writer's next call is
`JournalSealed`; a raw `sqlite3` insert on the sealed file is refused by the trigger), the
restore *refuses* while the writer's connection is still open (`-shm` present) and deletes
nothing, then succeeds after the writer closes (resuming at the seal), the retired file is
read-only and holds the original head, the restored journal carries its `restored_from` row
with both heads, and: the pending operation's artefact, issued *before* the backup and
consumed inside the window, is `approval_already_used` against the restored journal (the
`lost_consumption` row), while a fresh artefact for it is `approval_valid`; the post-snapshot
operation's artefact is `approval_not_applicable` then, after the intent is re-issued,
`approval_already_used` if it was consumed in the window and `approval_valid` if it was not; a
signed pair spent in the window is `replayed_call`. A second test restores with
`--original-lost` and asserts `approval_lost_window` on a pre-restore artefact and
`request_in_lost_window` on an envelope inside the quarantine — the loss is closed by the
mechanism, and the tests say which mechanism. Further tests: `restore` refused when `SRC` fails
verification (nothing sealed, asserted); refused when the lineage record differs; refused when
`ORIGINAL`'s `journal_id` differs; refused when the checkpoint reports `busy`; a sealed journal
refuses every write on every path (registry-only opens included) and still derives.

## 3. Rollover

A journal has a stated capacity (`journal.md`: derived-trace events ≤ 5,000,000, checked
under the write lock; `mcp-runtime.md` *Segmentation*). Today the journal refuses at
capacity and `serve` reports it; there is no continuity procedure. Rollover applies **from
schema 9 onward**: the schema-9 bump itself sends every schema-8 journal through the
earlier-schema procedure (README), since a schema-9 build cannot open one and cannot write a
predecessor record into a file it may not open. From schema 9:

0. **Check everything, change nothing.** Before the seal: `OLD` derives and verifies (a
   journal that fails an invariant is not sealed; the digest recorded later is recomputed after
   the seal, the pass/fail is known now); the operator's principal is live in `OLD` (the seal's
   `by`); `NEW`, `NEW-wal`, `NEW-shm` absent; every refusal `create` could
   make is checked now (token key readable and matching `OLD`'s check value, currencies decode,
   approver keys canonical, every policy-named approver live in `OLD`); `OLD` unsealed or sealed
   with this successor and no `NEW`. The seal is then the first irreversible step and nothing
   after it can refuse on a fact known before it.
1. **Seal.** `ledgergate journal rollover OLD NEW --policy … --token-key-file …` opens
   `OLD` under the components `serve` would use (a non-declarative policy set exists only as a
   digest in the definition, so the set comes from the flags and its digest must match `OLD`'s,
   as at `open`; the token key likewise), generates `NEW`'s `journal_id`, and in one `BEGIN
   IMMEDIATE` appends `lineage(sealed, successor_journal_id, at_sequence=head, by, at)`. From
   this commit `OLD` is frozen; a running `serve` on it is refused at its next transaction.
   An `OLD` sealed with *this* successor and no `NEW` file is a crashed rollover and resumes at
   step 2, creating `NEW` with the sealed id (`create` gains a `journal_id=` parameter for
   exactly this). Resume judges `NEW`'s absence at the path the operator names, and a sealed
   `OLD` cannot record where `NEW` went: two resumes with two `NEW` paths would be two
   journals under one id. Resume therefore requires an explicit `--resume`, and the rule that
   the operator issues it once rests on the operator, stated under *does not claim*.
2. **Then derive the frozen journal** and verify it; the trace now includes the seal, so its
   digest is the one `verify --chain` will recompute.
3. Creates `NEW` in **one transaction** with the sealed `journal_id` (`journal.md`'s
   definition text, "128 random bits generated at creation", is amended for this one path), `OLD`'s
   chart, `OLD`'s definition currency registry as the whole registry (`create` gains
   `registry=`, replacing rather than merging over the running build's bundled table, so `NEW`
   accepts exactly what `OLD` did), the
   policy set and token key from the flags, `approvers=` the approvers live in `OLD` at the
   sealed head as `create` seeds, and the `lineage(predecessor, journal_id=OLD's, head_sequence,
   head_hash, trace_digest, by=<the bootstrap principal's name>, at)` row written by `create`
   itself after the bootstrap `principal_events` row (the trigger requires a live transport
   `by`), so `NEW` exists only with its predecessor named. The operator's own name is `create`'s bootstrap principal;
   the other live transport and signed principals are then added, each `add` attributed to the
   operator and idempotent on re-run (a name already live is skipped), so `NEW`'s trace says who
   carried them over and from where. **Resume**: `OLD` sealed with successor *S* and a `NEW`
   whose `journal_id` is *S* → add the missing principals; without `NEW` → create it.
4. `ledgergate verify --chain OLD.json NEW.json` checks that `NEW`'s predecessor row names
   `OLD`'s id, head and trace digest, that `OLD` verifies, and that `OLD`'s last lineage row is
   the seal naming `NEW`'s id. The **trace digest** of a whole-journal derivation is defined in
   `trace-v2.md` at implementation as the JCS digest of the v2 document with `trace_id` removed
   (derivation is deterministic; `trace_id` is the one caller-chosen field).

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
| write-lock wait | time to acquire `BEGIN IMMEDIATE` | any wait over `BUSY_TIMEOUT_SECONDS / 2`; a `SQLITE_BUSY` refusal is logged as an unrecorded failure by error class and id *kind* only, never the caller's id (`mcp-runtime.md`, the stderr rule: stderr sits outside the redactor) |
| integrity | `PRAGMA quick_check` on `status`; any `IntegrityError` under `serve` | any |
| registry | live principals/approvers, pending operations, stranded lines (`journal pending`) | any stranded operation |
| clock | `requested_at` monotonicity across the last N invocations | a step backwards |

`serve` gains `--checkpoint-every N` (default 1,000 invocations) running
`PRAGMA wal_checkpoint(TRUNCATE)` on its own connection **in autocommit, between
invocations** (a checkpoint inside `BEGIN IMMEDIATE` is refused by SQLite with
`SQLITE_LOCKED`), logging the returned `(busy, log, checkpointed)` triple; the WAL alert fires
when `busy = 1` on consecutive checkpoints (a reader pinning the WAL) or the WAL exceeds the
size threshold. The clock alert is over the process's *own* clock readings (its last N `requested_at` values), not the journal's mixed history. Alerts are
lines on stderr in one fixed grammar (`ledgergate alert <name> <value> <threshold>`), which
is what an operator's log shipper matches; there is no built-in pager integration, stated.

## 5. Load and crash-recovery testing

- **Load** (`tests/integration/test_load.py`, marked `slow`, run nightly): stdio has one
  client (`mcp-runtime.md`), so load is a single client issuing 20,000 mixed calls (posts, reads, approvals,
  signed requests, replays) against one journal with the tokenizing admitter; assertions:
  the derived trace verifies, throughput is *printed* (not asserted; the number is information), the WAL stays
  under the threshold with checkpoints on, and `status` at the end matches the trace.
- **Crash recovery** (`tests/integration/test_crash.py`): a subprocess `serve` is killed with
  `SIGKILL` at a random point in a scripted sequence, ten times; the harness issues calls
  strictly one in flight; after each kill the journal opens, `PRAGMA integrity_check` is `ok`,
  the derived trace verifies, every served response's `call_id` is present as a committed
  invocation (the harness runs the identity admitter, so call ids are not tokenized), and the committed invocations are exactly the served ones plus at most the one
  call in flight at the kill (nothing beyond it) — the durability claim `journal.md` makes,
  exercised. Power loss is **not** simulated (a stated limit; SQLite's `synchronous = FULL`
  is the mechanism relied on, and the test proves process death only).
- **Concurrency** (extends the existing concurrent-writer tests): two `serve` processes on
  one journal with the busy timeout, interleaved presentations of one artefact for one pending
  operation: exactly one `approval_valid`, the other `replay` with verdict
  `approval_not_applicable` (consumption leaves the operation terminal, `journal.md`), exactly
  one `approval_consumptions` row, and one `signed_calls` spend per verified envelope.

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
   and the same for the sdist; `python -c "import importlib.metadata as m; print(m.metadata('ledgergate')['License-Expression'])"`
   prints `BUSL-1.1` (`pip show` renders that field only on pip versions that know Metadata 2.4).
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
- Single writable lineage across a rollover `--resume`: a sealed `OLD` cannot record where
  `NEW` was created, so a second `--resume` at another path would be a second journal under the
  successor id; the flag is explicit and the rule rests on the operator.
- Single writable lineage under `--original-lost`: the original is not sealed (it is gone), so
  a second backup of the lost lineage is a second promotable file, and only the operator's
  discipline (and the seal each promotion puts on its own `SRC`) stands between them.
- Power-loss durability beyond what `synchronous = FULL` provides; only process death is
  tested.
- Pager or metrics-system integration; alerts are log lines in a fixed grammar.
- A migration of any kind between schemas.
