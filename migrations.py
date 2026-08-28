"""
Versioned SQLite schema migrations for the tape administration database.

Design:
  - Every schema change is a numbered, ordered entry in MIGRATIONS below,
    each with a stable `version` string, a human `description`, and an
    `apply(conn)` function that performs the change.
  - Applied migrations are recorded in a `schema_migrations` table, one row
    per version, with the *real* timestamp of when it was actually run
    against that specific database (this is the practical answer to
    "version by date": the version id itself is a simple sequence number
    (stable, sortable, easy to reference), and `applied_at` is the actual
    date each one landed on this database).
  - `run_pending()` applies whatever hasn't been recorded yet, in order,
    each inside its own transaction. This is safe to call every time the
    program starts (see db.connect()), and also from the standalone
    `migrate.py` tool for an explicit, auditable upgrade step.
  - Every `apply()` function is idempotent (checks before creating a table/
    column/index rather than assuming a clean slate). This matters for the
    very first time a pre-existing production database -- created by an
    older, unversioned build of this tool -- is opened with this version:
    the tables/columns already exist, so each migration's checks make it a
    no-op, but it still gets correctly recorded in schema_migrations so
    every later startup is a simple, cheap "anything pending?" check
    instead of re-inspecting the whole schema every time.

To add a new schema change in the future: write a new `_apply_00NN(conn)`
function, add a `Migration(...)` entry at the end of MIGRATIONS with the
next version number, and never edit or reorder the existing entries.
"""

import sqlite3
from collections import namedtuple
from datetime import datetime, timezone

Migration = namedtuple("Migration", ["version", "description", "apply"])


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Introspection helpers -- used to make every migration idempotent
# ---------------------------------------------------------------------------

def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _index_exists(conn, index):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (index,)
    ).fetchone() is not None


def _column_exists(conn, table, column):
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


# ---------------------------------------------------------------------------
# Migrations, in order. Never reorder or remove an existing entry -- add new
# ones at the end.
# ---------------------------------------------------------------------------

def _apply_0001_initial_schema(conn):
    """Base tables: tapes (location tracking) and events (audit log)."""
    if not _table_exists(conn, "tapes"):
        conn.execute("""
            CREATE TABLE tapes (
                label               TEXT PRIMARY KEY,
                uuid                TEXT,
                pool                TEXT,
                pbs_status          TEXT,
                pbs_expired         INTEGER DEFAULT 0,
                media_location      TEXT NOT NULL DEFAULT 'unknown',
                changer_slot        INTEGER,
                export_requested    INTEGER DEFAULT 0,
                volume_statistics   TEXT,
                cartridge_memory    TEXT,
                first_seen          TEXT,
                last_seen_in_library TEXT,
                last_export_time    TEXT,
                last_return_time    TEXT,
                last_overdue_notify TEXT,
                notes               TEXT,
                updated_at          TEXT
            )
        """)
    if not _table_exists(conn, "events"):
        conn.execute("""
            CREATE TABLE events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                label       TEXT,
                event_type  TEXT NOT NULL,
                detail      TEXT,
                created_at  TEXT NOT NULL
            )
        """)


def _apply_0002_events_created_at_index(conn):
    """Index for efficient event-log retention purges."""
    if not _index_exists(conn, "idx_events_created_at"):
        conn.execute("CREATE INDEX idx_events_created_at ON events (created_at)")


def _apply_0003_drive_diagnostics_columns(conn):
    """Per-tape drive diagnostics captured on export: TapeAlert flags,
    medium wearout, volume error counters, drive status snapshot, and
    last-cleaned timestamp."""
    new_columns = {
        "drive_status": "TEXT",
        "alert_flags_raw": "TEXT",
        "alert_flags_decoded": "TEXT",
        "medium_wearout_pct": "REAL",
        "volume_error_counters": "TEXT",
        "last_cleaned": "TEXT",
    }
    for column, coltype in new_columns.items():
        if not _column_exists(conn, "tapes", column):
            conn.execute(f"ALTER TABLE tapes ADD COLUMN {column} {coltype}")


MIGRATIONS = [
    Migration("0001", "Initial schema: tapes + events tables", _apply_0001_initial_schema),
    Migration("0002", "Add index on events.created_at for retention purges", _apply_0002_events_created_at_index),
    Migration("0003", "Add drive diagnostics columns to tapes (alert flags, wearout, error counters, cleaning)",
               _apply_0003_drive_diagnostics_columns),
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def ensure_migrations_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     TEXT PRIMARY KEY,
            description TEXT,
            applied_at  TEXT NOT NULL
        )
    """)
    conn.commit()


def applied_versions(conn):
    ensure_migrations_table(conn)
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def current_version(conn):
    """The most recent migration version applied to this database, in
    MIGRATIONS order (not just DB insertion order, in case of clock skew),
    or None if no migrations have been applied yet."""
    applied = applied_versions(conn)
    latest = None
    for m in MIGRATIONS:
        if m.version in applied:
            latest = m.version
    return latest


def pending_migrations(conn):
    applied = applied_versions(conn)
    return [m for m in MIGRATIONS if m.version not in applied]


def run_pending(conn, verbose=False):
    """Apply every migration not yet recorded as applied, in order, each in
    its own transaction. Returns the list of Migration objects that were
    actually applied during this call (empty if already up to date)."""
    ensure_migrations_table(conn)
    applied_now = []

    for migration in MIGRATIONS:
        if migration.version in applied_versions(conn):
            continue
        if verbose:
            print(f"[migrate] applying {migration.version}: {migration.description}")
        try:
            migration.apply(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, description, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.description, _now_iso()),
            )
            conn.commit()
            applied_now.append(migration)
        except sqlite3.Error:
            conn.rollback()
            raise

    if verbose and not applied_now:
        v = current_version(conn)
        print(f"[migrate] database schema up to date (version {v})")

    return applied_now
