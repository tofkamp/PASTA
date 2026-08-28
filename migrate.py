#!/usr/bin/env python3
"""
Standalone schema migration tool for the tape administration database.

The main program (tape_admin.py) also checks and applies pending migrations
automatically every time it starts (via db.connect()), so day-to-day use
never requires running this manually. This tool exists for production
upgrades where you want an explicit, auditable step -- e.g. run this once
during a maintenance window right after deploying new code and before the
next cron-triggered `tape_admin.py run`, so you can see exactly what
changed and confirm it succeeded before anything else touches the database.

Never deletes or recreates the database -- every migration only adds
tables/columns/indexes, never drops data, and each one is safe to run
against a database that's already partway (or fully) upgraded.

Usage:
  ./migrate.py -d tapes.db              # apply any pending migrations
  ./migrate.py -d tapes.db --status     # show applied/pending, change nothing
  ./migrate.py -d tapes.db --dry-run    # show what WOULD run, change nothing
"""

import argparse
import sqlite3
import sys

import migrations


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-d", "--db", default="tapes.db", help="path to the SQLite database")
    parser.add_argument("--status", action="store_true", help="show applied/pending migrations and exit")
    parser.add_argument("--dry-run", action="store_true", help="show pending migrations without applying them")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        migrations.ensure_migrations_table(conn)
        applied = migrations.applied_versions(conn)
        pending = migrations.pending_migrations(conn)

        if args.status:
            print(f"Database: {args.db}")
            print(f"Schema version: {migrations.current_version(conn) or '(none applied yet)'}")
            print(f"\nApplied migrations ({len(applied)}):")
            rows = conn.execute("SELECT version, description, applied_at FROM schema_migrations ORDER BY applied_at").fetchall()
            if not rows:
                print("  (none)")
            for row in rows:
                print(f"  [{row['applied_at']}] {row['version']}: {row['description']}")
            print(f"\nPending migrations ({len(pending)}):")
            if not pending:
                print("  (none -- up to date)")
            for m in pending:
                print(f"  {m.version}: {m.description}")
            return

        if not pending:
            print(f"Database schema is already up to date (version {migrations.current_version(conn)}).")
            return

        if args.dry_run:
            print(f"{len(pending)} pending migration(s) would be applied to {args.db}:")
            for m in pending:
                print(f"  {m.version}: {m.description}")
            print("\nRe-run without --dry-run to apply.")
            return

        print(f"Applying {len(pending)} pending migration(s) to {args.db}...")
        applied_now = migrations.run_pending(conn, verbose=True)
        print(f"\nDone. Schema version is now {migrations.current_version(conn)}.")
        if not applied_now:
            print("(nothing was applied -- another process may have already upgraded it)")
    except sqlite3.Error as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
