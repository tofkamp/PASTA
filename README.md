# PBS Assistance Service for Tape Administration
(PBS Tape Administration Tool)

Tracks tape media leaving and returning to your Proxmox Backup Server tape
library, stages exported tapes in the changer's mailslots, and emails the
operator when tapes need to be picked up or are overdue to come back.

Features
- Selects and move tapes to mailslots for external storage
- Monitors single or multiple mediapools for full tapes
- Send mail requesting which tapes to return
- Monitor read/write errors
- Monitor wearing of tapes
- Autoclean tape drive if needed
- Upgradable database
- Only TapeReader permission needed
 
## Setup

### 1. Create the PBS user, API token, and permissions

Run these on the PBS host itself (as root, or via `pveproxy`/`pbs` shell
access). 

```bash
# create a dedicated user to hold the token
proxmox-backup-manager user create tape-admin@pbs --email tape-admin@example.com

# create the API token (the secret is only ever shown here -- save it now)
proxmox-backup-manager user generate-token tape-admin@pbs tape-reader \
    --comment "Tape administration API token"
# -> {"tokenid": "tape-admin@pbs!tape-reader", "value": "<TOKEN-SECRET>"}

# grant the token the TapeReader role on /tape
proxmox-backup-manager acl update /tape TapeReader --auth-id 'tape-admin@pbs!tape-reader'
```


### 2. Configure the tool

```bash
pip install -r requirements.txt --break-system-packages   # or use a venv
cp config.example.ini config.ini
```

Edit `config.ini`:

- `[pbs]` — host, the `user` (`tape-admin@pbs`) and `token_id` (just the
  token's own name, `tape-reader` — not the full `user!token` string) from
  step 1, the `changer`/`drive` identifiers as named in PBS, and the list of
  **mailslot (import/export) slot numbers**. You can see slot numbers with
  `proxmox-tape changer status` on the PBS host, or in the GUI under Tape
  Backup -> Changer -> Status.
- `[policy]` — pool selection for auto-export, GFS-aware overdue handling,
  auto-clean behavior, wearout thresholds, and event log retention. See
  "GFS / multi-pool support" below.
- `[email]` — SMTP settings for the operator notifications.


## Commands

```bash
./tape_admin.py sync              # reconcile DB against what's physically in the library
./tape_admin.py mark-export A00001L8   # manually flag a tape to be pulled next run
./tape_admin.py remove-tape A00001L8 "broken - drive jam"   # stop tracking a broken/stolen/lost/replaced tape
./tape_admin.py events            # show recent event log (see options below)
./tape_admin.py process-exports   # load/read/unload flagged tapes into mailslots, email operator
./tape_admin.py check-overdue     # email reminder for tapes out too long
./tape_admin.py run               # do all of the above, one digest email
./tape_admin.py status            # dump current DB state (includes schema version)
```

`remove-tape LABEL REASON` permanently stops tracking a tape locally --
for tapes that are broken, stolen, lost, or replaced and will never come
back. It requires a reason (recorded in the event log) and, by default,
keeps that tape's past event history for an audit trail (pass
`--purge-events` to also wipe that). This only affects local tracking --
if the tape still has a media entry in PBS, that's a separate cleanup
(e.g. `proxmox-tape media destroy`).

`events` shows the audit log (exports, arrivals, pickups, drive alerts,
cleaning cycles, removals, errors, ...) directly from the SQLite database,
most recent first, since querying `tapes.db` by hand isn't exactly
convenient. Filter with `--label LABEL`, `--type EVENT_TYPE` (e.g.
`error`, `drive_alert`, `removed`), and `--limit N` (default 50, `0` for
everything).

## Suggested cron

```cron
45 7 * * *  cd /opt/tape_admin && ./tape_admin.py run        >> run.log 2>&1
0 8   * * *   cd /opt/tape_admin && ./tape_admin.py check-overdue >> run.log 2>&1
```

Running `run` frequently means arrivals and pickups get detected (and
reflected in the DB / logged) quickly, without spamming email — a digest is
only sent when there's something to report, and overdue reminders are only
re-sent once every `notify_cooldown_days` (default 1 day) per tape.

## How tape lifecycle is tracked

```
unknown -> in_library -> exported_pending_pickup -> external -> returned -> in_library -> ...
```

- **in_library**: seen in a changer slot or drive right now.
- **exported_pending_pickup**: we just loaded it, captured its status, and
  unloaded it into a mailslot — waiting for the operator to physically take it.
- **external**: it's no longer anywhere in the changer (operator took it, or
  it went missing outside our export flow — either way, per your spec, we
  assume it's out of the building).
- **returned**: was external, now seen back in the library (this transition
  is what triggers the "tape has arrived" notification/log entry). It flips
  to `in_library` proper on the next sync.

## API request timeout

`config.ini [pbs] api_timeout_seconds` (default 60, and enforced to never
go below 60 regardless of what's configured) is passed straight to
proxmoxer's `ProxmoxAPI` client, which applies it to every single API
call made through it -- changer status, `transfer`, load/unload,
volume-statistics, all of it. proxmoxer's own default is only 5 seconds,
which is far too short once you're moving physical media around instead
of just reading data.

## A note on endpoint names

`pbs_client.py`'s methods are named to mirror the `proxmox-tape` CLI
subcommands, which are thin wrappers over the exact same REST endpoints
(e.g. `load-media-from-slot` -> `POST /tape/drive/<id>/load-slot`). If your
PBS version has renamed or restructured any of these, check the API Viewer
built into the PBS web UI and adjust the path segments in `pbs_client.py`
accordingly — everything PBS-specific is isolated in that one file.

## GFS / multi-pool support

PBS's own `expired` flag on a media entry means that specific tape's
*pool* retention period has passed — and since it's read per-tape from
`/tape/media/list`, it's inherently pool-specific. That's exactly what a
GFS (Grandfather-Father-Son) setup needs: daily/weekly/monthly/yearly
pools each with their own retention, and each tape's return timing
following whichever pool it belongs to, not one global rule.

Two settings in `[policy]` are built around this:

- **`auto_export_pools`** decides which pools get their full tapes
  auto-flagged for export (in addition to always being able to flag any
  tape manually with `mark-export`):
  - `All` (case-insensitive) — every pool
  - `pool1,pool2,...` — only the named pools (e.g. just the pools meant to
    leave the building: `offsite-monthly,offsite-yearly`)
  - left empty — auto-flagging off entirely, export only ever happens via
    `mark-export`
- **`return_after_days`** is *not* the primary trigger for "this tape needs
  to come back" — that's each pool's own `expired` flag, checked per tape.
  It's an optional **hard cap** on top of that: if set above `0`, a tape
  gets nagged about once external that many days regardless of whether its
  pool has expired yet (a safety net for pools with very long or no
  retention). Set it to `0` to disable the cap and rely purely on each
  pool's own expiration — appropriate once every pool you use has a
  sensible retention configured in PBS itself.

The overdue digest section shows *why* each tape was flagged (`reason:
pool retention expired`, `reason: external > 90d (hard cap)`, or both), so
it stays legible even with several pools mixed together.

## Drive diagnostics captured on every export

Whenever a tape is loaded for export, before it's unloaded to a mailslot,
`process-exports` also captures `GET /tape/drive/{drive}/status` (while the
tape is loaded, so the readings are specific to that tape/drive pairing) and
stores it alongside volume-statistics and cartridge-memory. Three things are
pulled out of that for reporting:

- **`alert-flags`** — decoded from the raw `TapeAlertFlags(0x...)` value
  against the standard SCSI/SSC TapeAlert flag table (in `pbs_client.py`).
  Any non-zero value is logged as a `drive_alert` event and shown in the
  digest email. If the flags include "Clean Now" or "Clean Periodic", and
  `auto_clean_drive = true` in `config.ini`, the tool runs a cleaning cycle
  (`proxmox-tape clean`) right after the tape is safely unloaded. With
  `auto_clean_drive = false` (the default) it only logs/emails a warning and
  leaves cleaning to the operator. Note: this flag table is the generic
  industry standard — most drives implement a subset, and bit assignment can
  vary by vendor, so cross-check against your drive's documentation if a
  flag looks off.
- **`medium-wearout`** — reported as a percentage (`value * 100`). The
  digest shows a **WARNING** between `wearout_warn_pct` and
  `wearout_error_pct` (default 80–100%), and an **ALERT** above
  `wearout_error_pct` (i.e. the tape has exceeded its rated life).
- **volume-statistics error counters** — any field from
  `volume-statistics` whose name contains `error` (e.g.
  `write-error-count`, `permanent-read-error-count`) is pulled out and
  listed per-tape in the digest.

All of this is stored per-tape in the `tapes` table (`drive_status`,
`alert_flags_raw`, `alert_flags_decoded`, `medium_wearout_pct`,
`volume_error_counters`, `last_cleaned`), so you can query history later
even if the operator has already taken the tape offsite.

## Waiting for the drive to be ready

Loading a tape, and unloading one back to a mailslot, are both mechanical
(robot pick/insert/spin-up, or arm move back) and not instant. So
`load_media()`/`load_from_slot()` (waiting for the drive to be *ready*) and
`unload()` (waiting for the drive to be *empty*) both poll `GET
/tape/drive/{drive}/status` afterwards until `drive-activity` reports
`"no-activity"`, up to `drive_ready_attempts` times (`config.ini [pbs]`,
default 20). Each attempt **sleeps first** (`drive_ready_delay_seconds`,
default 5s) and only then polls — so the first check happens 5s after the
load/unload call, not immediately — for up to 100s total by default. If
the drive never settles, a `TimeoutError` is raised, which
`process-exports` catches, logs as an `error` event for that tape, and
moves on to the next flagged tape rather than aborting the whole run.

Every poll prints the `drive-activity` value (e.g. `[drive-poll 3/20,
waiting for ready] drive-activity='no-activity'`), so you can watch actual
hardware behavior in the logs.

## Event log retention

The `events` table is an audit trail (exports, arrivals, pickups, drive
alerts, cleaning cycles, errors, ...) and grows forever unless pruned.
Every `run` (and the standalone `cleanup-events` command, if you'd rather
schedule it separately) deletes events older than `event_retention_days`
in `config.ini [policy]` (default 365). The `tapes` table itself is never
pruned — only the log — so tape history/status is retained indefinitely.

## Upgrading in production: schema versioning & migrations

Every schema change is a numbered entry in `migrations.py` (`MIGRATIONS`),
each with a version (`"0001"`, `"0002"`, ...), a description, and an
`apply(conn)` function. Applied versions are recorded in a
`schema_migrations` table inside the database itself, with the real
timestamp of when each one actually ran on that database.

**On every startup**, `db.connect()` (used by `tape_admin.py` for every
command) checks this table and applies whatever's pending, in order,
before anything else touches the database — so you never have to think
about it for day-to-day cron use, and the database is never deleted or
recreated to pick up a new version.

For production upgrades where you'd rather see an explicit, auditable step
before the next scheduled run touches the database, use the standalone
tool:

```bash
./migrate.py -d tapes.db --status    # see current version, applied & pending migrations
./migrate.py -d tapes.db --dry-run   # see what WOULD run, without applying anything
./migrate.py -d tapes.db             # apply pending migrations
```

Typical production upgrade: deploy the new code, run `./migrate.py -d
tapes.db` once during a maintenance window, confirm the printed schema
version, then let cron resume as normal.

**Adding a new schema change later:** write a new `_apply_00NN(conn)`
function in `migrations.py`, add a `Migration(...)` entry at the end of
`MIGRATIONS` with the next version number, and never edit or reorder the
existing entries. Every `apply()` function checks whether its table/
column/index already exists before creating it, which is what makes it
safe to run against a database that's already partway upgraded (or, the
first time you run this versioned code, a database created by an older,
unversioned build — the existing tables/columns are detected and skipped,
but still get correctly recorded as applied).

## Refusing to touch a drive that's already in use

Before `process-exports` (and therefore `run`) does anything, it checks
whether the configured drive already has a tape loaded (via the changer's
own view of drive occupancy, not the drive-status endpoint, since that's
the more reliable source). If it does, the whole export step is aborted
immediately — nothing is loaded, unloaded, or flagged as handled — on the
assumption that something else (a backup or restore job) may currently be
using the drive. This is logged as a `drive_busy` event and printed to
stderr; any tapes still flagged `export_requested` simply get picked up on
the next run once the drive is free again.

## Returned too early

When `sync` sees a tape transition from `external` back to `in_library`, it
checks two things about that tape: whether PBS considers it `expired`
(retention passed), and whether we've ever sent an overdue reminder for it
(`last_overdue_notify`). If neither is true — the tape isn't expired *and*
nobody asked for it back — it's flagged `too_early` and shows up as a
warning line under "Tapes detected back in the library" in the digest
email, so an operator returning tapes prematurely (e.g. before their
retention/offsite window is up) gets caught rather than silently accepted.

## Returned tapes inserted into a mailslot

An operator returning a tape will most naturally just drop it into a
mailslot, not open the library and place it directly on a storage shelf.
`sync` handles this: any tape found sitting in one of the configured
`export_slots` that we did **not** put there ourselves (i.e. its tracked
state isn't `exported_pending_pickup`) is treated as a returned/new tape
that needs to rejoin the library. It's moved into the first free storage
slot with a direct changer `transfer` (`POST /tape/changer/{name}/transfer`
— a robot-arm slot-to-slot move) — **no drive load, read, or mount is
involved**. If it was previously tracked as `external`, this triggers the
same "returned" logic (and `too_early` check) as a tape found directly in
a storage slot; if it's a tape we've never seen before, it's just quietly
shelved as `in_library` with no return notification. If there's no free
storage slot to move it to, the tape is left exactly where it is, an
`error` event is logged, and its tracked location is left unchanged (not
incorrectly marked "returned") — `sync` will pick it up again once a slot
frees up.

## Verbose output

`config.ini [general] verbose` (default `true`) controls whether progress
messages — sync transitions, each drive status poll during a wait loop,
export steps — are printed to stdout. Set it to `false` for quiet cron runs
where you only care about the log file and the email digest; errors and
warnings are still printed either way.
