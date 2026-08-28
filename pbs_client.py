"""
Thin wrapper around the PBS tape API, via proxmoxer.

Endpoint names below mirror the `proxmox-tape` CLI subcommands 1:1 (that CLI
is itself a thin wrapper over these same REST endpoints), e.g.:

    proxmox-tape load-media-from-slot <slot> --drive X
        -> POST /tape/drive/X/load-slot   {"source-slot": slot}

    proxmox-tape unload --drive X --target-slot N
        -> POST /tape/drive/X/unload      {"target-slot": N}

    proxmox-tape cartridge-memory --drive X
        -> GET  /tape/drive/X/cartridge-memory

    proxmox-tape volume-statistics --drive X
        -> GET  /tape/drive/X/volume-statistics

    proxmox-tape changer status NAME
        -> GET  /tape/changer/NAME/status

If your PBS version renamed any of these, the API Viewer in the web UI
(Help -> API Viewer, or https://<host>:8007/docs/api-viewer/) is the
authoritative source -- adjust the path segments below to match.

proxmoxer does NOT auto-convert underscores to hyphens. Path segments with a
literal hyphen (which Python's dotted attribute syntax can't express, since
`.load-slot` would be parsed as subtraction) are added via proxmoxer's
"string notation" call form instead: `resource("load-slot")`. Plain
`prox.tape.drive(name)` is how you address a path *parameter* (the drive id),
which is a different mechanism from addressing a hyphenated path *segment*.
"""

from proxmoxer import ProxmoxAPI

import re
import time


MIN_API_TIMEOUT = 60


def connect(host, token_name, token_value, user, verify_ssl=True, port=8007, timeout=60):
    """
    token_name: the token's own name, e.g. 'tape-reader' (also accepts the
                full 'user!token' form -- only the part after '!' is used)
    token_value: the secret returned when the token was created
    user: the user part, e.g. 'tape-admin@pbs'
    timeout: request timeout in seconds, applied by proxmoxer to every API
             call made through this client (proxmoxer's own default is a
             mere 5s, far too short for tape operations like a changer
             transfer or a drive load, which can legitimately take a
             while). Enforced to be at least MIN_API_TIMEOUT regardless of
             what's passed in.
    """
    timeout = max(timeout, MIN_API_TIMEOUT)
    return ProxmoxAPI(
        host,
        user=user,
        token_name=token_name.split("!")[-1],
        token_value=token_value,
        service="PBS",
        port=port,
        verify_ssl=verify_ssl,
        timeout=timeout,
    )


class PBSTape:
    def __init__(self, prox, drive, changer, drive_ready_attempts=20, drive_ready_delay=5, verbose=True):
        self.prox = prox
        self.drive = drive
        self.changer = changer
        self.drive_ready_attempts = drive_ready_attempts
        self.drive_ready_delay = drive_ready_delay
        self.verbose = verbose

    # ---- read-only status ----------------------------------------------

    def media_list(self, pool=None):
        params = {}
        if pool:
            params["pool"] = pool
        return self.prox.tape.media.list.get(**params)

    def changer_status(self, use_cache=False):
        """Returns slot/drive occupancy for the configured changer."""
        return self.prox.tape.changer(self.changer).status.get(cache=use_cache)

    def drive_status(self):
        return self.prox.tape.drive(self.drive).status.get()

    def drive_occupant_label(self):
        """Returns the label-text of whatever tape is currently loaded in
        this drive, or None if the drive is empty, using the changer's own
        view (more reliable than guessing at drive-status field names)."""
        status = self.changer_status()
        _, in_drive = find_label_in_changer_status(status)
        for label, drive_id in in_drive.items():
            if drive_id == self.drive:
                return label
        return None

    def _wait_for_drive_state(self, what):
        """
        Generic poll loop used for both 'wait until loaded/ready' and 'wait
        until unloaded/empty'. Per spec: each attempt first sleeps
        `drive_ready_delay` seconds, *then* polls drive status -- not the
        other way around, since the mechanical action needs time before the
        first poll is even worth making.

        Readiness is decided purely by `drive-activity` reporting
        'no-activity' (the drive isn't busy loading/unloading/positioning
        any more). `what` is only used for logging/error messages ('ready',
        'empty') -- the check itself is the same for both cases.

        Returns the last status dict once ready. Raises TimeoutError if it
        never is within `drive_ready_attempts` tries.
        """
        last_exc = None
        last_status = None
        for attempt in range(1, self.drive_ready_attempts + 1):
            time.sleep(self.drive_ready_delay)
            try:
                status = self.drive_status()
                last_status = status
                activity = status.get("drive-activity") or status.get("drive_activity")
                if self.verbose:
                    print(f"[drive-poll {attempt}/{self.drive_ready_attempts}, waiting for {what}] "
                          f"drive-activity={activity!r}")
                if activity == "no-activity":
                    return status
            except Exception as exc:  # noqa: BLE001 - drive genuinely not ready yet
                last_exc = exc
                if self.verbose:
                    print(f"[drive-poll {attempt}/{self.drive_ready_attempts}, waiting for {what}] "
                          f"status call failed: {exc}")

        if last_status is not None:
            # We got status responses throughout, just never saw
            # 'no-activity' -- return the last reading rather than failing
            # outright, since the caller can still decide what to do with it.
            return last_status
        raise TimeoutError(
            f"drive {self.drive} did not become {what} after "
            f"{self.drive_ready_attempts} attempts ({self.drive_ready_attempts * self.drive_ready_delay}s): {last_exc}"
        )

    def wait_for_drive_ready(self):
        """Wait until the drive reports drive-activity == 'no-activity'
        (a load has finished and it's usable)."""
        return self._wait_for_drive_state("ready")

    def wait_for_drive_empty(self):
        """Wait until an unload has actually finished, using the same
        drive-activity == 'no-activity' signal."""
        return self._wait_for_drive_state("empty")

    # ---- drive operations -----------------------------------------------

    def load_from_slot(self, slot):
        # dotted notation can't express a hyphen ("load-slot" would be parsed
        # as subtraction), so the hyphenated segment is passed as a string
        # call instead: resource("load-slot") -> .../load-slot
        self.prox.tape.drive(self.drive)("load-slot").post(**{"source-slot": slot})
        return self.wait_for_drive_ready()

    def load_media(self, label_text):
        self.prox.tape.drive(self.drive)("load-media").post(**{"label-text": label_text})
        return self.wait_for_drive_ready()

    def unload(self, target_slot=None):
        params = {}
        if target_slot is not None:
            params["target-slot"] = target_slot
        self.prox.tape.drive(self.drive).unload.post(**params)
        return self.wait_for_drive_empty()

    def volume_statistics(self):
        return self.prox.tape.drive(self.drive)("volume-statistics").get()

    def cartridge_memory(self):
        return self.prox.tape.drive(self.drive)("cartridge-memory").get()

    def clean_drive(self):
        """Run a cleaning cycle (proxmox-tape clean). The drive must be
        empty; PBS locates the library's cleaning cartridge itself."""
        return self.prox.tape.drive(self.drive).clean.post()

    def transfer(self, from_slot, to_slot):
        """Move a tape directly between two changer slots via the robot
        arm -- no drive involved, so no read/mount happens. Used to move a
        returned tape out of a mailslot into a proper storage slot."""
        return self.prox.tape.changer(self.changer).transfer.post(
            **{"from": from_slot, "to": to_slot}
        )


def find_label_in_changer_status(status):
    """
    Parse the changer status payload into {label_text: slot_number} for
    everything currently physically present (storage slots + import/export
    slots), and separately report what's loaded in drives.

    The exact JSON shape can vary a bit by PBS version, so this is written
    defensively: it walks the returned list and pulls out whichever of
    'label-text' / 'slot' / 'drive-id' fields are present.
    """
    present = {}   # label -> slot number
    in_drive = {}  # label -> drive id

    for entry in status:
        label = entry.get("label-text") or entry.get("label_text")
        if not label:
            continue
        if "drive-id" in entry or "drive_id" in entry:
            in_drive[label] = entry.get("drive-id") or entry.get("drive_id")
        else:
            slot = entry.get("entry-id") or entry.get("slot") or entry.get("entry_id")
            present[label] = slot

    return present, in_drive


def find_free_export_slot(status, export_slots):
    """Given the changer status and the configured list of mailslot numbers,
    return the first one that's currently empty."""
    occupied_slots = set()
    for entry in status:
        slot = entry.get("entry-id") or entry.get("slot") or entry.get("entry_id")
        label = entry.get("label-text") or entry.get("label_text")
        if slot is not None and label:
            occupied_slots.add(slot)
    for slot in export_slots:
        if slot not in occupied_slots:
            return slot
    return None


def find_free_storage_slot(status, export_slots):
    """The mirror of find_free_export_slot: return the first empty *storage*
    slot (i.e. any slot the changer reports that is NOT one of the
    configured mailslot numbers), for relocating a returned tape out of a
    mailslot into the library proper."""
    occupied_slots = set()
    all_slots = set()
    for entry in status:
        slot = entry.get("entry-id") or entry.get("slot") or entry.get("entry_id")
        if slot is None:
            continue
        all_slots.add(slot)
        label = entry.get("label-text") or entry.get("label_text")
        if label:
            occupied_slots.add(slot)
    storage_slots = sorted(s for s in all_slots if s not in export_slots)
    for slot in storage_slots:
        if slot not in occupied_slots:
            return slot
    return None


# ---------------------------------------------------------------------------
# Drive diagnostics: TapeAlert flags, wearout, error counters
# ---------------------------------------------------------------------------

# Standard SCSI/SSC TapeAlert log page (0x2E) flag definitions. Flag number N
# corresponds to bit (N-1) in the bitmap. This is the generic industry-standard
# table (per the "TapeAlert Technology" spec used across LTO/SCSI drives) --
# most drives implement a subset of these, so treat unset/absent bits as
# "not applicable" rather than "fine". Verify against your drive vendor's
# documentation if a flag you'd expect isn't showing up.
TAPE_ALERT_FLAGS = {
    1: "Read Warning", 2: "Write Warning", 3: "Hard Error", 4: "Media",
    5: "Read Failure", 6: "Write Failure", 7: "Media Life", 8: "Not Data Grade",
    9: "Write Protect", 10: "No Removal", 11: "Cleaning Media",
    12: "Unsupported Format", 13: "Recoverable Mechanical Cartridge Failure",
    14: "Unrecoverable Mechanical Cartridge Failure",
    15: "Memory Chip in Cartridge Failure", 16: "Forced Eject",
    17: "Read Only Format", 18: "Tape Directory Corrupted on Load",
    19: "Nearing Media Life", 20: "Clean Now", 21: "Clean Periodic",
    22: "Expired Cleaning Media", 23: "Invalid Cleaning Tape",
    24: "Retension Requested", 25: "Dual Port Interface Error",
    26: "Cooling Fan Failing", 27: "Power Supply", 28: "Power Consumption",
    29: "Drive Maintenance", 30: "Hardware A", 31: "Hardware B",
    32: "Interface", 33: "Eject Media", 34: "Microcode Update Fail",
    35: "Drive Humidity", 36: "Drive Temperature", 37: "Drive Voltage",
    38: "Predictive Failure", 39: "Diagnostics Required",
    50: "Lost Statistics", 51: "Tape Directory Invalid at Unload",
    52: "Tape System Area Write Failure", 53: "Tape System Area Read Failure",
    54: "No Start of Data", 55: "Loading Failure",
    56: "Unrecoverable Unload Failure", 57: "Automation Interface Failure",
    58: "Firmware Failure", 59: "WORM Medium - Integrity Check Failed",
    60: "WORM Medium - Overwrite Attempted",
}

# Flags that indicate the drive wants a cleaning cycle.
CLEANING_FLAG_NUMBERS = {20, 21}  # "Clean Now", "Clean Periodic"


def parse_alert_flags(raw):
    """
    raw is the string PBS returns, e.g. 'TapeAlertFlags(0x0)' or
    'TapeAlertFlags(0x180000)'. Returns (int_value, [flag names], needs_cleaning).
    """
    if not raw:
        return 0, [], False
    match = re.search(r"0x[0-9a-fA-F]+", str(raw))
    if not match:
        return 0, [], False
    value = int(match.group(0), 16)
    names = [name for num, name in TAPE_ALERT_FLAGS.items() if value & (1 << (num - 1))]
    needs_cleaning = any(value & (1 << (num - 1)) for num in CLEANING_FLAG_NUMBERS)
    return value, names, needs_cleaning


def parse_wearout_pct(drive_status):
    """medium-wearout in the API is a fraction (e.g. 0.42); the caller asked
    for it reported as a percentage, i.e. multiplied by 100."""
    raw = drive_status.get("medium-wearout") or drive_status.get("medium_wearout")
    if raw is None:
        return None
    try:
        return float(raw) * 100
    except (TypeError, ValueError):
        return None


def extract_error_counters(volume_statistics):
    """Pull out any field that looks like an error counter from the
    volume-statistics (SCSI log page 17h) response, for reporting. Field
    names in this endpoint's response are prefixed with 'volume_' (unlike
    the hyphenated names elsewhere in the API) -- that prefix is stripped
    since it doesn't add information once we're already labeling this as
    "volume error counters"."""
    result = {}
    for k, v in volume_statistics.items():
        if "error" not in k.lower():
            continue
        result[k] = v
    return result
