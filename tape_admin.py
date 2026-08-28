#!/usr/bin/env python3
"""
Tape administration tool for Proxmox Backup Server.

Subcommands:
  sync              Reconcile changer/media state into the local DB
                     (detects tapes going external / coming back).
  mark-export LABEL Flag a tape to be pulled out and moved to a mailslot
                     on the next 'process-exports' run.
  process-exports   Load each flagged tape, capture status/volume-statistics/
                     cartridge-memory, unload it to a free mailslot, and
                     email the operator to come fetch it.
  check-overdue     Email a reminder for tapes that have been external
                     longer than `return_after_days`.
  run               sync + auto-flag + process-exports + check-overdue,
                     in one go, with a single digest email. Meant for cron.
  status            Print the current DB state.

Typical cron setup:
  */15 * * * *  tape_admin.py run        # frequent: catch arrivals/exports fast
  0 8   * * *   tape_admin.py check-overdue   # daily overdue reminder

Configuration lives in config.ini (see config.example.ini).
"""

import argparse
import configparser
import sys
from datetime import datetime, timezone

import db
import mailer
import pbs_client


def load_config(path):
    cfg = configparser.ConfigParser()
    if not cfg.read(path):
        sys.exit(f"Could not read config file: {path}")
    return cfg


def connect_pbs(cfg):
    p = cfg["pbs"]
    prox = pbs_client.connect(
        host=p["host"],
        token_name=p["token_id"],
        token_value=p["token_secret"],
        user=p["user"],
        verify_ssl=p.getboolean("verify_ssl", fallback=True),
        port=p.getint("port", fallback=8007),
        timeout=p.getint("api_timeout_seconds", fallback=30),
    )
    return pbs_client.PBSTape(
        prox,
        drive=p["drive"],
        changer=p["changer"],
        drive_ready_attempts=p.getint("drive_ready_attempts", fallback=20),
        drive_ready_delay=p.getint("drive_ready_delay_seconds", fallback=5),
        verbose=get_verbose(cfg),
    )


def get_verbose(cfg):
    return cfg["general"].getboolean("verbose", fallback=True) if cfg.has_section("general") else True


# ---------------------------------------------------------------------------
# sync: reconcile changer contents against DB
# ---------------------------------------------------------------------------

def do_sync(conn, tape, cfg, verbose=True):
    arrived = []
    export_slots = [int(s) for s in cfg["pbs"]["export_slots"].split(",")]

    # 1. Pull in everything PBS knows about each tape (pool, status, expired flag)
    for entry in tape.media_list():
        label = entry.get("label-text") or entry.get("label_text")
        if not label:
            continue
        db.upsert_tape_known(
            conn,
            label,
            uuid=entry.get("uuid"),
            pool=entry.get("pool"),
            pbs_status=entry.get("status"),
            pbs_expired=entry.get("expired", False),
        )

    # 2. What's physically present in the library right now?
    status = tape.changer_status()
    present, in_drive = pbs_client.find_label_in_changer_status(status)

    # 2b. A tape sitting in a mailslot that we did NOT put there ourselves
    # (i.e. it's not one we're staging for pickup) means an operator has
    # physically returned a tape by inserting it into a mailslot. Move it
    # into an empty storage slot via a direct changer transfer -- no drive
    # involved, so no read/mount happens -- so it actually rejoins the
    # library instead of sitting in a mailslot indefinitely (which would
    # also make that mailslot unavailable for the next export).
    known_before = {t["label"]: t for t in db.all_tapes(conn)}
    stuck_in_mailslot = set()  # relocation failed -- must NOT be treated as a normal 'present' tape below
    for label, slot in list(present.items()):
        if slot not in export_slots:
            continue
        row = known_before.get(label)
        if row is not None and row["media_location"] == "exported_pending_pickup":
            continue  # we deliberately staged this one for pickup -- leave it alone

        free_slot = pbs_client.find_free_storage_slot(status, export_slots)
        if free_slot is None:
            db.log_event(conn, label, "error",
                          f"tape found in mailslot {slot} but no free storage slot to relocate it to")
            print(f"[sync] ERROR: {label} is in mailslot {slot} but there's no free storage slot", file=sys.stderr)
            stuck_in_mailslot.add(label)
            continue

        was_external = row is not None and row["media_location"] == "external"
        if verbose:
            print(f"[sync] {label}: found in mailslot {slot} (returned tape), "
                  f"moving to storage slot {free_slot}")
        tape.transfer(slot, free_slot)
        db.log_event(conn, label, "moved_from_mailslot",
                      f"moved from mailslot {slot} to storage slot {free_slot}")

        if row is None:
            db.ensure_tape_row(conn, label)
            db.log_event(conn, label, "inserted", "first seen via mailslot, moved into library")

        if was_external:
            too_early = not row["pbs_expired"] and not row["last_overdue_notify"]
            db.set_location(conn, label, "returned", slot=free_slot,
                             extra_sql=", last_return_time = ?", extra_params=(db.now_iso(),))
            db.log_event(conn, label, "returned", "tape detected back in library (via mailslot)")
            arrived.append({"label": label, "pool": row.get("pool"), "too_early": too_early})
            if verbose and too_early:
                print(f"[sync] {label}: WARNING: returned too early")
            if too_early:
                db.log_event(conn, label, "returned_too_early",
                              "not PBS-expired and never sent an overdue notice for this tape")
        else:
            db.set_location(conn, label, "in_library", slot=free_slot,
                             extra_sql=", last_seen_in_library = ?", extra_params=(db.now_iso(),))

        # our view of the changer is now stale for this slot -- reflect the move
        present[label] = free_slot

    # 3. Reconcile everything else against current (possibly just-updated) state.
    # Tapes stuck in a mailslot (relocation failed above) are deliberately
    # excluded here -- they're physically present, but not actually "back
    # in the library" in any meaningful sense, so leave their tracked state
    # as-is (still 'external') until a free storage slot lets sync move them.
    present_labels = (set(present) | set(in_drive)) - stuck_in_mailslot
    known = {t["label"]: t for t in db.all_tapes(conn)}

    for label, t in known.items():
        loc = t["media_location"]

        if label in present_labels:
            slot = present.get(label)
            if loc in ("external",):
                db.set_location(conn, label, "returned", slot=slot,
                                 extra_sql=", last_return_time = ?", extra_params=(db.now_iso(),))
                db.log_event(conn, label, "returned", "tape detected back in library")

                # "Too early" = PBS doesn't consider it expired yet, and we
                # never sent an overdue reminder asking for it back -- i.e.
                # nobody asked for this tape, and its retention hasn't
                # passed, yet it showed up in the library anyway.
                too_early = not t["pbs_expired"] and not t["last_overdue_notify"]
                arrived.append({"label": label, "pool": t.get("pool"), "too_early": too_early})
                if verbose:
                    tag = " (WARNING: returned too early)" if too_early else ""
                    print(f"[sync] {label}: external -> returned{tag}")
                if too_early:
                    db.log_event(conn, label, "returned_too_early",
                                  "not PBS-expired and never sent an overdue notice for this tape")
            elif loc in ("unknown", "returned"):
                db.set_location(conn, label, "in_library", slot=slot,
                                 extra_sql=", last_seen_in_library = ?", extra_params=(db.now_iso(),))
            else:
                # already in_library / exported_pending_pickup and still present -> just refresh timestamp
                db.set_location(conn, label, loc, slot=slot,
                                 extra_sql=", last_seen_in_library = ?", extra_params=(db.now_iso(),))
        else:
            if loc in ("in_library", "exported_pending_pickup"):
                db.set_location(conn, label, "external")
                event = "picked_up" if loc == "exported_pending_pickup" else "went_external"
                db.log_event(conn, label, event, "tape no longer found in changer")
                if verbose:
                    print(f"[sync] {label}: {loc} -> external ({event})")
            # if already 'external' or 'unknown', nothing to do

    return arrived


# ---------------------------------------------------------------------------
# auto-flagging based on pool policy
# ---------------------------------------------------------------------------

def get_auto_export_pool_filter(cfg):
    """
    Parses config.ini [policy] auto_export_pools. Three forms:
      - 'All' (case-insensitive) -> matches every pool; returns None
      - 'pool1,pool2,...'        -> matches only those pools; returns a set
      - '' (empty/absent)        -> auto-flagging disabled entirely (manual
                                     export only, via `mark-export`);
                                     returns an empty frozenset (falsy on
                                     membership, distinguishable from None)

    Useful for GFS-style setups with several pools of different retention
    (e.g. daily/weekly/monthly/yearly) where you might want every pool
    auto-flagged when full ('All'), only the offsite-bound ones by name, or
    none at all if you'd rather always pick tapes by hand.
    """
    raw = cfg["policy"].get("auto_export_pools", "").strip()
    if not raw:
        return frozenset()
    if raw.lower() == "all":
        return None  # sentinel: matches any pool
    return {p.strip() for p in raw.split(",") if p.strip()}


def _pool_matches(pool_filter, pool_name):
    """pool_filter is None ('All'), a set of names, or an empty frozenset
    (nothing auto-selected)."""
    if pool_filter is None:
        return True
    return pool_name in pool_filter


def auto_flag_exports(conn, cfg):
    pool_filter = get_auto_export_pool_filter(cfg)
    if pool_filter is not None and not pool_filter:
        return  # manual-only: nothing configured for auto-export
    for t in db.all_tapes(conn):
        if (
            _pool_matches(pool_filter, t["pool"])
            and t["pbs_status"] == "full"
            and t["media_location"] == "in_library"
            and not t["export_requested"]
        ):
            db.mark_export_requested(conn, t["label"], True)
            db.log_event(conn, t["label"], "auto_flagged", f"full in auto-export pool {t['pool']}")
            print(f"[auto-flag] {t['label']} flagged for export (pool {t['pool']} is full)")


# ---------------------------------------------------------------------------
# process-exports: physically pull flagged tapes and stage them in mailslots
# ---------------------------------------------------------------------------

def do_process_exports(conn, tape, cfg, verbose=True):
    export_slots = [int(s) for s in cfg["pbs"]["export_slots"].split(",")]
    auto_clean = cfg["policy"].getboolean("auto_clean_drive", fallback=False)
    wearout_warn = cfg["policy"].getfloat("wearout_warn_pct", fallback=80.0)
    wearout_error = cfg["policy"].getfloat("wearout_error_pct", fallback=100.0)

    exported = []
    cleaned = []

    # Refuse to touch the drive if something is already loaded -- it may be
    # a backup or restore job in progress that we'd otherwise interrupt.
    occupant = tape.drive_occupant_label()
    if occupant:
        msg = (f"drive '{tape.drive}' already has tape '{occupant}' loaded -- "
               f"another process (backup/restore?) may be using it; refusing to touch the drive")
        db.log_event(conn, occupant, "drive_busy", msg)
        print(f"[export] ABORTED: {msg}", file=sys.stderr)
        return exported, cleaned

    flagged = [t for t in db.all_tapes(conn) if t["export_requested"] and t["media_location"] == "in_library"]
    if not flagged:
        return exported, cleaned

    for t in flagged:
        label = t["label"]
        try:
            if verbose:
                print(f"[export] loading {label} into drive (waiting for it to be ready)...")
            drive_status = tape.load_media(label)

            vol_stats = tape.volume_statistics()
            cart_mem = tape.cartridge_memory()

            alert_raw = drive_status.get("alert-flags") or drive_status.get("alert_flags")
            alert_value, alert_names, needs_cleaning = pbs_client.parse_alert_flags(alert_raw)
            wearout_pct = pbs_client.parse_wearout_pct(drive_status)
            error_counters = pbs_client.extract_error_counters(vol_stats)

            if alert_value != 0:
                db.log_event(conn, label, "drive_alert", f"{alert_raw}: {', '.join(alert_names)}")
                print(f"[export] WARNING {label}: drive alert flags set -> {alert_raw} ({', '.join(alert_names)})")

            status = tape.changer_status()
            free_slot = pbs_client.find_free_export_slot(status, export_slots)
            if free_slot is None:
                db.log_event(conn, label, "error", "no free mailslot available")
                print(f"[export] ERROR: no free mailslot for {label}, leaving in drive")
                continue

            if verbose:
                print(f"[export] unloading {label} to mailslot {free_slot}...")
            tape.unload(target_slot=free_slot)

            db.record_export(
                conn, label, free_slot, vol_stats, cart_mem,
                drive_status=drive_status,
                alert_flags_raw=alert_raw,
                alert_flags_decoded=alert_names,
                medium_wearout_pct=wearout_pct,
                volume_error_counters=error_counters,
            )

            exported.append({
                "label": label,
                "pool": t.get("pool"),
                "slot": free_slot,
                "alert_flags_raw": alert_raw,
                "alert_flags_decoded": alert_names,
                "wearout_pct": wearout_pct,
                "wearout_warn": wearout_pct is not None and wearout_warn <= wearout_pct <= wearout_error,
                "wearout_error": wearout_pct is not None and wearout_pct > wearout_error,
                "error_counters": error_counters,
            })

            # The drive is now empty (tape unloaded to its mailslot), so it's
            # safe to run a cleaning cycle if the alert flags called for one.
            if needs_cleaning:
                if auto_clean:
                    try:
                        if verbose:
                            print(f"[export] alert flags indicate cleaning needed, running clean cycle...")
                        tape.clean_drive()
                        db.record_cleaning(conn, label)
                        cleaned.append({"label": label, "alert_flags_raw": alert_raw})
                    except Exception as clean_exc:  # noqa: BLE001
                        db.log_event(conn, label, "error", f"auto-clean failed: {clean_exc}")
                        print(f"[export] ERROR: auto-clean failed: {clean_exc}", file=sys.stderr)
                else:
                    db.log_event(conn, label, "drive_alert",
                                  "cleaning needed but auto_clean_drive is disabled in config")

        except Exception as exc:  # noqa: BLE001 - we want to log and keep going
            db.log_event(conn, label, "error", f"export failed: {exc}")
            print(f"[export] ERROR exporting {label}: {exc}", file=sys.stderr)

    return exported, cleaned


# ---------------------------------------------------------------------------
# overdue check
# ---------------------------------------------------------------------------

def do_check_overdue(conn, cfg, notify_cooldown_days=1):
    """
    A tape's actual return date is governed by its own pool's retention
    policy in PBS (reflected here as `pbs_expired`, which is pool-specific
    -- exactly what you want for a GFS setup with daily/weekly/monthly/
    yearly pools each on a different schedule). `return_after_days` is now
    just an optional hard cap on top of that: if set (> 0), a tape gets
    nagged about once it's been external that many days *regardless* of
    whether its pool considers it expired yet -- a safety net for pools
    with very long or no retention. Set it to 0 to disable the cap
    entirely and rely purely on each pool's own expiration.
    """
    max_days = cfg["policy"].getint("return_after_days", fallback=0)
    now = datetime.now(timezone.utc)
    overdue = []

    for t in db.all_tapes(conn):
        if t["media_location"] != "external" or not t["last_export_time"]:
            continue
        exported_at = datetime.fromisoformat(t["last_export_time"])
        days_out = (now - exported_at).days

        pool_expired = bool(t["pbs_expired"])
        cap_tripped = max_days > 0 and days_out >= max_days
        if not pool_expired and not cap_tripped:
            continue  # this pool's retention hasn't passed, and no hard cap configured/tripped

        last_notify = t["last_overdue_notify"]
        if last_notify:
            last_notify_dt = datetime.fromisoformat(last_notify)
            if (now - last_notify_dt).days < notify_cooldown_days:
                continue

        reasons = []
        if pool_expired:
            reasons.append("pool retention expired")
        if cap_tripped:
            reasons.append(f"external > {max_days}d (hard cap)")

        overdue.append({**t, "days_out": days_out, "reason": ", ".join(reasons)})
        with db.cursor(conn) as cur:
            cur.execute(
                "UPDATE tapes SET last_overdue_notify = ? WHERE label = ?",
                (db.now_iso(), t["label"]),
            )
        db.log_event(conn, t["label"], "overdue", f"{days_out} days external ({', '.join(reasons)})")

    return overdue


def technical_expired(conn):
    return [t for t in db.all_tapes(conn) if t["pbs_expired"] and t["media_location"] != "external"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_sync(args, cfg, conn, tape):
    arrived = do_sync(conn, tape, cfg, verbose=get_verbose(cfg))
    if arrived:
        body = mailer.build_digest([], arrived, [], [])
        if body and cfg["email"].getboolean("enabled", fallback=True):
            mailer.send_email(cfg["email"], "[Tape] Tapes returned to library", body)
    print(f"sync complete, {len(arrived)} tape(s) newly returned")


def cmd_mark_export(args, cfg, conn, tape):
    db.ensure_tape_row(conn, args.label)
    db.mark_export_requested(conn, args.label, True)
    print(f"{args.label} flagged for export")


def cmd_remove_tape(args, cfg, conn, tape):
    removed = db.remove_tape(conn, args.label, args.reason, purge_events=args.purge_events)
    if not removed:
        print(f"{args.label} is not tracked -- nothing to remove")
        return
    note = " (event history purged)" if args.purge_events else " (event history kept)"
    print(f"{args.label} removed from tracking: {args.reason}{note}")
    print("Note: this only stops local tracking -- if the tape still has a "
          "media entry in PBS itself, deal with that separately (e.g. "
          "'proxmox-tape media destroy').")


def cmd_events(args, cfg, conn, tape):
    events = db.list_events(conn, label=args.label, event_type=args.type, limit=args.limit)
    if not events:
        print("no matching events")
        return
    width = max(len(e["event_type"]) for e in events)
    for e in events:
        label = e["label"] or "-"
        detail = e["detail"] or ""
        print(f"{e['created_at']}  {e['event_type']:<{width}}  {label:<12}  {detail}")


def cmd_process_exports(args, cfg, conn, tape):
    exported, cleaned = do_process_exports(conn, tape, cfg, verbose=get_verbose(cfg))
    if exported or cleaned:
        body = mailer.build_digest(exported, [], [], [], cleaned)
        if body and cfg["email"].getboolean("enabled", fallback=True):
            mailer.send_email(cfg["email"], "[Tape] Tapes ready for pickup", body)
    print(f"{len(exported)} tape(s) exported to mailslots, {len(cleaned)} cleaning cycle(s) run")


def cmd_check_overdue(args, cfg, conn, tape):
    overdue = do_check_overdue(conn, cfg)
    expired = technical_expired(conn)
    if overdue or expired:
        body = mailer.build_digest([], [], overdue, expired)
        if body and cfg["email"].getboolean("enabled", fallback=True):
            mailer.send_email(cfg["email"], "[Tape] Overdue / expired tapes", body)
    print(f"{len(overdue)} overdue, {len(expired)} technically expired")


def cmd_cleanup_events(args, cfg, conn, tape):
    retention_days = cfg["policy"].getint("event_retention_days", fallback=365)
    deleted = db.purge_old_events(conn, retention_days)
    print(f"purged {deleted} event(s) older than {retention_days} days")


def cmd_run(args, cfg, conn, tape):
    verbose = get_verbose(cfg)
    arrived = do_sync(conn, tape, cfg, verbose=verbose)
    auto_flag_exports(conn, cfg)
    exported, cleaned = do_process_exports(conn, tape, cfg, verbose=verbose)
    overdue = do_check_overdue(conn, cfg)
    expired = technical_expired(conn)

    retention_days = cfg["policy"].getint("event_retention_days", fallback=365)
    purged = db.purge_old_events(conn, retention_days)
    if purged:
        print(f"purged {purged} event(s) older than {retention_days} days")

    body = mailer.build_digest(exported, arrived, overdue, expired, cleaned)
    if body and cfg["email"].getboolean("enabled", fallback=True):
        mailer.send_email(cfg["email"], "[Tape] Tape administration report", body)
        print("digest email sent")
    else:
        print("nothing to report")


def cmd_status(args, cfg, conn, tape):
    print(f"schema version: {db.schema_version(conn)}")
    tapes = db.all_tapes(conn)
    if not tapes:
        print("no tapes tracked yet -- run 'sync' first")
        return
    width = max(len(t["label"]) for t in tapes)
    for t in tapes:
        print(
            f"{t['label']:<{width}}  pool={t.get('pool') or '-':<12} "
            f"pbs_status={t.get('pbs_status') or '-':<10} "
            f"location={t['media_location']:<22} "
            f"export_req={bool(t['export_requested'])}"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", "--config", default="config.ini")
    parser.add_argument("-d", "--db", default="tapes.db")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sync")
    p_mark = sub.add_parser("mark-export")
    p_mark.add_argument("label")
    p_remove = sub.add_parser("remove-tape", help="permanently stop tracking a tape (broken/stolen/lost/replaced)")
    p_remove.add_argument("label")
    p_remove.add_argument("reason", help="short reason, recorded in the event log, e.g. 'broken', 'stolen', 'lost', 'replaced'")
    p_remove.add_argument("--purge-events", action="store_true",
                           help="also delete this tape's event history (default: keep it for audit purposes)")
    p_events = sub.add_parser("events", help="show the event log")
    p_events.add_argument("--label", help="only show events for this tape")
    p_events.add_argument("--type", help="only show events of this type (e.g. exported, error, drive_alert)")
    p_events.add_argument("--limit", type=int, default=50, help="max rows to show, 0 for no limit (default: 50)")
    sub.add_parser("process-exports")
    sub.add_parser("check-overdue")
    sub.add_parser("cleanup-events")
    sub.add_parser("run")
    sub.add_parser("status")

    args = parser.parse_args()
    cfg = load_config(args.config)
    # db.connect() checks the schema version and applies any pending
    # migrations before returning -- this happens before any other code
    # touches the database, on every startup.
    conn = db.connect(args.db, verbose=get_verbose(cfg))
    try:
        tape = connect_pbs(cfg)

        handlers = {
            "sync": cmd_sync,
            "mark-export": cmd_mark_export,
            "remove-tape": cmd_remove_tape,
            "events": cmd_events,
            "process-exports": cmd_process_exports,
            "check-overdue": cmd_check_overdue,
            "cleanup-events": cmd_cleanup_events,
            "run": cmd_run,
            "status": cmd_status,
        }
        handlers[args.command](args, cfg, conn, tape)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
