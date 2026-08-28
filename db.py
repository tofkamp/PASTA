"""SQLite persistence layer for tape administration."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import migrations


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path, verbose=False):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrations.run_pending(conn, verbose=verbose)
    return conn


def schema_version(conn):
    return migrations.current_version(conn)


@contextmanager
def cursor(conn):
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    finally:
        cur.close()


def log_event(conn, label, event_type, detail=""):
    with cursor(conn) as cur:
        cur.execute(
            "INSERT INTO events (label, event_type, detail, created_at) VALUES (?, ?, ?, ?)",
            (label, event_type, detail, now_iso()),
        )


def get_tape(conn, label):
    row = conn.execute("SELECT * FROM tapes WHERE label = ?", (label,)).fetchone()
    return dict(row) if row else None


def all_tapes(conn):
    rows = conn.execute("SELECT * FROM tapes ORDER BY label").fetchall()
    return [dict(r) for r in rows]


def upsert_tape_known(conn, label, uuid=None, pool=None, pbs_status=None, pbs_expired=None):
    """Merge in facts PBS knows about a tape (from /tape/media/list), without
    touching our own location-tracking fields."""
    existing = get_tape(conn, label)
    ts = now_iso()
    with cursor(conn) as cur:
        if existing is None:
            cur.execute(
                """INSERT INTO tapes (label, uuid, pool, pbs_status, pbs_expired,
                                       media_location, first_seen, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'unknown', ?, ?)""",
                (label, uuid, pool, pbs_status, int(bool(pbs_expired)), ts, ts),
            )
            log_event(conn, label, "inserted", "first seen via media list")
        else:
            cur.execute(
                """UPDATE tapes SET uuid = COALESCE(?, uuid),
                                     pool = COALESCE(?, pool),
                                     pbs_status = COALESCE(?, pbs_status),
                                     pbs_expired = ?,
                                     updated_at = ?
                   WHERE label = ?""",
                (uuid, pool, pbs_status, int(bool(pbs_expired)), ts, label),
            )


def set_location(conn, label, location, slot=None, extra_sql="", extra_params=()):
    ts = now_iso()
    with cursor(conn) as cur:
        cur.execute(
            f"""UPDATE tapes SET media_location = ?, changer_slot = ?, updated_at = ? {extra_sql}
                WHERE label = ?""",
            (location, slot, ts, *extra_params, label),
        )


def mark_export_requested(conn, label, requested=True):
    with cursor(conn) as cur:
        cur.execute(
            "UPDATE tapes SET export_requested = ?, updated_at = ? WHERE label = ?",
            (int(requested), now_iso(), label),
        )


def record_export(conn, label, slot, volume_statistics, cartridge_memory,
                   drive_status=None, alert_flags_raw=None, alert_flags_decoded=None,
                   medium_wearout_pct=None, volume_error_counters=None):
    ts = now_iso()
    with cursor(conn) as cur:
        cur.execute(
            """UPDATE tapes
               SET media_location = 'exported_pending_pickup',
                   changer_slot = ?,
                   export_requested = 0,
                   volume_statistics = ?,
                   cartridge_memory = ?,
                   drive_status = ?,
                   alert_flags_raw = ?,
                   alert_flags_decoded = ?,
                   medium_wearout_pct = ?,
                   volume_error_counters = ?,
                   last_export_time = ?,
                   updated_at = ?
               WHERE label = ?""",
            (
                slot,
                json.dumps(volume_statistics),
                json.dumps(cartridge_memory),
                json.dumps(drive_status) if drive_status is not None else None,
                alert_flags_raw,
                json.dumps(alert_flags_decoded) if alert_flags_decoded is not None else None,
                medium_wearout_pct,
                json.dumps(volume_error_counters) if volume_error_counters is not None else None,
                ts, ts, label,
            ),
        )
    log_event(conn, label, "exported", f"unloaded to slot {slot}")


def record_cleaning(conn, label):
    """Note that a cleaning cycle was triggered while handling this tape's
    export (the cleaning itself is a drive-level action, but we log it
    against the tape whose alert flags triggered it for traceability)."""
    with cursor(conn) as cur:
        cur.execute(
            "UPDATE tapes SET last_cleaned = ?, updated_at = ? WHERE label = ?",
            (now_iso(), now_iso(), label),
        )
    log_event(conn, label, "drive_cleaned", "auto-clean triggered by TapeAlert flags")


def purge_old_events(conn, retention_days):
    """Delete events older than retention_days. created_at is stored as
    ISO-8601 UTC, which sorts/compares correctly as a plain string, so no
    date parsing is needed for the cutoff comparison."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds")
    with cursor(conn) as cur:
        cur.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
        return cur.rowcount


def ensure_tape_row(conn, label):
    if get_tape(conn, label) is None:
        ts = now_iso()
        with cursor(conn) as cur:
            cur.execute(
                """INSERT INTO tapes (label, media_location, first_seen, updated_at)
                   VALUES (?, 'unknown', ?, ?)""",
                (label, ts, ts),
            )


def remove_tape(conn, label, reason, purge_events=False):
    """Completely remove a tape from active tracking (broken, stolen, lost,
    replaced, ...). This does NOT touch PBS itself -- the tape's media
    entry there, if any, must be dealt with separately (e.g.
    `proxmox-tape media destroy`).

    By default the tape's row is deleted from `tapes` (so it stops showing
    up in status/digests/overdue checks) but its `events` history is kept,
    with one final 'removed' event recording the reason -- so there's still
    an audit trail of what happened and when. Pass purge_events=True to
    also delete that history if you want no trace left at all.

    Returns True if a tape was actually removed, False if the label wasn't
    tracked in the first place.
    """
    if get_tape(conn, label) is None:
        return False

    log_event(conn, label, "removed", reason)

    with cursor(conn) as cur:
        cur.execute("DELETE FROM tapes WHERE label = ?", (label,))
        if purge_events:
            cur.execute("DELETE FROM events WHERE label = ?", (label,))

    return True


def list_events(conn, label=None, event_type=None, limit=50):
    """Query the events audit log, most recent first. limit=0 means no limit."""
    query = "SELECT * FROM events WHERE 1=1"
    params = []
    if label:
        query += " AND label = ?"
        params.append(label)
    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)
    query += " ORDER BY created_at DESC, id DESC"
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in conn.execute(query, params).fetchall()]
