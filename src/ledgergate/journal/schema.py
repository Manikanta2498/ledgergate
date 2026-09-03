# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""The journal's SQLite schema, exactly as ``docs/spec/journal.md`` specifies it.

Everything the specification says the schema enforces is enforced here, not in Python:
one global sequence through the ``journal`` allocator table; immediate foreign keys, so no
row can precede a row it references and the protocol's write order is one linearization of
that partial order; the outcome chain (one root per operation,
no forks, same-operation predecessor, only ``awaiting_approval`` has successors); the
``invocation_responses`` shape; single-use approvals; and no ``UPDATE`` or ``DELETE`` on
any fact table, ever.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# ruff: noqa: S608 - table names are interpolated from FACT_TABLES, a module constant, never input

SCHEMA_VERSION = 2  # 2: definition.token_check

FACT_TABLES = (
    "definition",
    "operations",
    "outcomes",
    "invocations",
    "invocation_responses",
    "decisions",
    "approvals",
    "approval_consumptions",
    "events",
    "reads",
)

_DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS journal (
    journal_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS definition (
    journal_sequence INTEGER PRIMARY KEY REFERENCES journal(journal_sequence),
    singleton INTEGER NOT NULL UNIQUE CHECK (singleton = 1),
    journal_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    codec_version TEXT NOT NULL,
    policy_set_version TEXT NOT NULL,
    token_domain TEXT NOT NULL,
    token_key_version TEXT NOT NULL,
    token_check TEXT NOT NULL,
    approval_key TEXT NOT NULL,
    chart TEXT NOT NULL,
    currencies TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    journal_sequence INTEGER PRIMARY KEY REFERENCES journal(journal_sequence),
    key TEXT NOT NULL UNIQUE,
    fingerprint TEXT NOT NULL,
    command TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invocations (
    journal_sequence INTEGER PRIMARY KEY REFERENCES journal(journal_sequence),
    operation INTEGER REFERENCES operations(journal_sequence),
    requested_at TEXT NOT NULL,
    principal TEXT NOT NULL,
    disposition TEXT NOT NULL
        CHECK (disposition IN ('new','replay','conflict','approval','read','invalid')),
    attempted_fingerprint TEXT,
    attempted_command TEXT,
    request_digest TEXT,
    call_id TEXT,
    CHECK ((disposition IN ('read','invalid')) = (operation IS NULL)),
    CHECK ((disposition = 'invalid') = (request_digest IS NULL))
);

CREATE TABLE IF NOT EXISTS approvals (
    journal_sequence INTEGER PRIMARY KEY REFERENCES journal(journal_sequence),
    invocation INTEGER NOT NULL REFERENCES invocations(journal_sequence),
    journal_id TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    approver TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    key TEXT NOT NULL,
    subject TEXT,
    amount TEXT,
    currency TEXT,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    signature TEXT NOT NULL,
    check_result TEXT NOT NULL CHECK (check_result IN (
        'checks_passed','approval_invalid','approval_expired',
        'approval_scope_mismatch','approval_not_applicable'))
);

CREATE TABLE IF NOT EXISTS approval_consumptions (
    journal_sequence INTEGER PRIMARY KEY REFERENCES journal(journal_sequence),
    approval_id TEXT NOT NULL UNIQUE,
    presentation INTEGER NOT NULL REFERENCES approvals(journal_sequence),
    invocation INTEGER NOT NULL REFERENCES invocations(journal_sequence)
);

CREATE TABLE IF NOT EXISTS decisions (
    journal_sequence INTEGER PRIMARY KEY REFERENCES journal(journal_sequence),
    invocation INTEGER NOT NULL REFERENCES invocations(journal_sequence),
    operation INTEGER REFERENCES operations(journal_sequence),
    context TEXT NOT NULL,
    policy_set_version TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('allow','deny','approval_required')),
    matched_rule TEXT NOT NULL,
    reason TEXT NOT NULL,
    presentation INTEGER REFERENCES approvals(journal_sequence),
    approval_verdict TEXT CHECK (approval_verdict IS NULL OR approval_verdict IN (
        'approval_valid','approval_already_used','approval_not_applicable',
        'approval_invalid','approval_expired','approval_scope_mismatch')),
    consumption INTEGER REFERENCES approval_consumptions(journal_sequence),
    CHECK ((presentation IS NULL) = (approval_verdict IS NULL))
);

CREATE TABLE IF NOT EXISTS outcomes (
    journal_sequence INTEGER PRIMARY KEY REFERENCES journal(journal_sequence),
    operation INTEGER NOT NULL REFERENCES operations(journal_sequence),
    previous_outcome INTEGER,
    outcome TEXT NOT NULL CHECK (outcome IN ('applied','rejected','denied','awaiting_approval')),
    error_type TEXT,
    error_message TEXT,
    entry_id TEXT,
    posted_at TEXT,
    head_before TEXT NOT NULL,
    head_after TEXT NOT NULL,
    ledger_sequence INTEGER NOT NULL,
    decision INTEGER REFERENCES decisions(journal_sequence),
    UNIQUE (journal_sequence, operation),
    UNIQUE (previous_outcome),
    FOREIGN KEY (previous_outcome, operation) REFERENCES outcomes(journal_sequence, operation),
    CHECK ((outcome = 'rejected') = (error_type IS NOT NULL)),
    CHECK ((outcome = 'rejected') = (error_message IS NOT NULL)),
    CHECK ((entry_id IS NULL) = (posted_at IS NULL)),
    CHECK (outcome = 'applied' OR entry_id IS NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS outcomes_one_root
    ON outcomes(operation) WHERE previous_outcome IS NULL;

CREATE TRIGGER IF NOT EXISTS outcomes_successor_of_pending
BEFORE INSERT ON outcomes WHEN NEW.previous_outcome IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'only awaiting_approval has successors')
    WHERE (SELECT outcome FROM outcomes WHERE journal_sequence = NEW.previous_outcome)
          <> 'awaiting_approval';
END;

CREATE TABLE IF NOT EXISTS invocation_responses (
    journal_sequence INTEGER PRIMARY KEY REFERENCES journal(journal_sequence),
    invocation INTEGER NOT NULL UNIQUE REFERENCES invocations(journal_sequence),
    disposition TEXT NOT NULL,
    outcome INTEGER REFERENCES outcomes(journal_sequence),
    response TEXT NOT NULL CHECK (response IN (
        'applied','rejected','denied','awaiting_approval','replayed','conflict','invalid','read')),
    CHECK ((outcome IS NOT NULL) = (disposition IN ('new','replay','approval')))
);

CREATE TRIGGER IF NOT EXISTS responses_disposition_matches
BEFORE INSERT ON invocation_responses
BEGIN
    SELECT RAISE(ABORT, 'response disposition differs from its invocation')
    WHERE (SELECT disposition FROM invocations WHERE journal_sequence = NEW.invocation)
          IS NOT NEW.disposition;
END;

CREATE TABLE IF NOT EXISTS events (
    journal_sequence INTEGER PRIMARY KEY REFERENCES journal(journal_sequence),
    invocation INTEGER REFERENCES invocations(journal_sequence),
    direction TEXT NOT NULL CHECK (direction IN ('inbound','outbound','message')),
    body TEXT NOT NULL,
    CHECK ((direction = 'message') = (invocation IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS events_one_per_direction
    ON events(invocation, direction) WHERE invocation IS NOT NULL;

CREATE TABLE IF NOT EXISTS reads (
    journal_sequence INTEGER PRIMARY KEY REFERENCES journal(journal_sequence),
    invocation INTEGER NOT NULL UNIQUE REFERENCES invocations(journal_sequence),
    cursor INTEGER NOT NULL,
    head TEXT NOT NULL,
    result_digest TEXT NOT NULL
);
"""


def _kind_trigger(table: str) -> str:
    assert table in FACT_TABLES  # names come from this module, never from input
    return f"""
CREATE TRIGGER IF NOT EXISTS {table}_kind
BEFORE INSERT ON {table}
BEGIN
    SELECT RAISE(ABORT, 'journal allocation consumed by the wrong table')
    WHERE (SELECT kind FROM journal WHERE journal_sequence = NEW.journal_sequence)
          IS NOT '{table}';
END;
"""


def _append_only_triggers(table: str) -> str:
    return f"""
CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table}
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
"""


def create_schema(conn: sqlite3.Connection) -> None:
    """Create every table, index and trigger, atomically. Idempotent.

    SQLite DDL is transactional, so a crash mid-way leaves the file either empty or
    complete, never a partial table set that both constructors would then refuse."""
    script = [_DDL.replace("PRAGMA foreign_keys = ON;", "")]
    for table in FACT_TABLES:
        script.append(_kind_trigger(table))
        script.append(_append_only_triggers(table))
    script.append(_append_only_triggers("journal"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("BEGIN;\n" + "\n".join(script) + "\nCOMMIT;")


BUSY_TIMEOUT_SECONDS = 5.0
"""How long a write waits for the lock before the attempt is an unrecorded failure."""


def tables_of(path: str) -> set[str]:
    """The table names in an existing SQLite file, read-only and without pragmas, so that
    inspecting a file changes nothing about it. Includes SQLite's own ``sqlite_*`` tables
    (``sqlite_sequence``, ``sqlite_stat1`` after ``ANALYZE``); callers exclude them.
    Raises ``sqlite3.Error`` if it is not a database or does not exist."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return {
            name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()


JOURNAL_TABLES = frozenset({"journal", *FACT_TABLES})


def probe(path: str) -> None:
    """Confirm, read-only, that ``path`` is a complete journal: every journal table and no
    other. A database missing any of them, or holding a stranger's table, is refused
    untouched. Raises ``ValueError`` for a database that is not a journal and
    ``sqlite3.Error`` for a file that is not a database."""
    tables = {name for name in tables_of(path) if not name.startswith("sqlite_")}
    if tables != JOURNAL_TABLES:
        raise ValueError("not a journal: table set differs from the journal schema")


def connect(path: str, *, create: bool = True) -> sqlite3.Connection:
    """Open the journal file with the pragmas the protocol depends on.

    With ``create=False`` a missing file is an error rather than a new empty database, so
    ``open()`` never manufactures a journal by accident.
    """
    if create:
        conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_SECONDS)
    else:
        uri = Path(path).resolve().as_uri() + "?mode=rw"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=BUSY_TIMEOUT_SECONDS)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = FULL")
    except sqlite3.Error:
        conn.close()  # a file that is not a database fails here, lazily
        raise
    return conn
