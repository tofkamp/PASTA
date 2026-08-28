"""Minimal SMTP mailer for operator notifications."""

import smtplib
from email.mime.text import MIMEText


def send_email(cfg, subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"]
    msg["To"] = cfg["to_addr"]

    with smtplib.SMTP(cfg["smtp_host"], int(cfg.get("smtp_port", 587))) as server:
        if cfg.getboolean("smtp_starttls", fallback=True):
            server.starttls()
        if cfg.get("smtp_user"):
            server.login(cfg["smtp_user"], cfg["smtp_password"])
        server.sendmail(cfg["from_addr"], [cfg["to_addr"]], msg.as_string())


def build_digest(exported, arrived, overdue, expired_technical, cleaned=None):
    """Compose a single plain-text digest email body from the events of one run."""
    cleaned = cleaned or []
    lines = []

    if exported:
        lines.append("=== Tapes ready for pickup (moved to mailslot) ===")
        for t in exported:
            lines.append(f"  {t['label']}  (pool: {t.get('pool','?')}, mailslot: {t['slot']})")

            wearout = t.get("wearout_pct")
            if wearout is not None:
                if t.get("wearout_error"):
                    lines.append(f"    ALERT: medium wearout {wearout:.1f}% (over rated life, consider retiring)")
                elif t.get("wearout_warn"):
                    lines.append(f"    WARNING: medium wearout {wearout:.1f}% (nearing end of rated life)")

            if t.get("alert_flags_decoded"):
                lines.append(f"    DRIVE ALERT ({t.get('alert_flags_raw')}): " + ", ".join(t["alert_flags_decoded"]))

            if t.get("error_counters"):
                nonzero = {k: v for k, v in t["error_counters"].items() if v not in (0, "0", None)}
                if nonzero:
                    counters = ", ".join(f"{k}={v}" for k, v in nonzero.items())
                    lines.append(f"    volume error counters: {counters}")
        lines.append("")

    if cleaned:
        lines.append("=== Drive cleaning cycles run automatically ===")
        for t in cleaned:
            lines.append(f"  triggered while handling {t['label']}  ({t.get('alert_flags_raw')})")
        lines.append("")

    if arrived:
        lines.append("=== Tapes detected back in the library ===")
        for t in arrived:
            lines.append(f"  {t['label']}  (pool: {t.get('pool','?')})")
            if t.get("too_early"):
                lines.append("    WARNING: returned too early -- not expired, and never requested back")
        lines.append("")

    if overdue:
        lines.append("=== Tapes overdue for return (external too long) ===")
        for t in overdue:
            lines.append(
                f"  {t['label']}  (pool: {t.get('pool','?')}, "
                f"external since: {t.get('last_export_time','unknown')}, "
                f"days out: {t.get('days_out','?')}, "
                f"reason: {t.get('reason','?')})"
            )
        lines.append("")

    if expired_technical:
        lines.append("=== Tapes PBS reports as expired (retention passed, reusable) ===")
        for t in expired_technical:
            lines.append(f"  {t['label']}  (pool: {t.get('pool','?')})")
        lines.append("")

    if not lines:
        return None

    return "\n".join(lines)
