"""Turn drill results into something a business owner can read in five seconds."""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import smtplib
from email.message import EmailMessage


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _duration(seconds: float) -> str:
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 120:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.0f} min"


HINTS = [
    ("end of file", "The backup file is incomplete. Usually the disk filled up or the backup job was killed half way."),
    ("truncated", "The backup file is incomplete. Usually the disk filled up or the backup job was killed half way."),
    ("not a valid archive", "This file is not a database backup. Check what your backup job is actually writing."),
    ("unsupported version", "The backup was made by a newer Postgres than the restore tools. Upgrade the drill image."),
    ("no backup files", "No backup was found at all. Your backup job may have stopped running."),
    ("written", "Your newest backup is too old. The nightly job has probably stopped."),
    ("expected at least", "The backup is much smaller than expected. Data may be missing."),
    ("rows fell", "A table lost a lot of rows since last week. Check for accidental deletes before it's too late."),
    ("newest record", "The newest data in the backup is too old. You would lose recent orders if you restored today."),
    ("table missing", "An important table is missing from the backup."),
]


def hint(detail: str) -> str | None:
    d = detail.lower()
    for needle, text in HINTS:
        if needle in d:
            return text
    return None


def summary(r: dict) -> str:
    if r["ok"]:
        rpo = r.get("newest_record_age_hours")
        rpo_txt = f" Newest data: {rpo:.1f}h old." if rpo is not None else ""
        return f"Your backup works. Restore took {_duration(r['restore_seconds'])}.{rpo_txt} Nothing missing."
    failed = [c for c in r["checks"] if not c["ok"]]
    return f"WARNING: your backup FAILED the drill ({len(failed)} problem{'s' if len(failed) != 1 else ''}). Fix this today."


def text(r: dict) -> str:
    lines = [
        f"Backup Doctor drill: {r['name']}  ({r['finished_at']})",
        ("PASS  " if r["ok"] else "FAIL  ") + summary(r),
        "",
        f"  Backup:  {r['backup']['source']}",
        f"  Size:    {_human_size(r['backup']['size_bytes'])}, written {r['backup']['age_hours']:.1f}h ago",
    ]
    if r.get("restore_seconds") is not None:
        lines.append(f"  Restore: {r['restore_seconds']:.1f}s (your real recovery time)")
    lines.append("")
    for c in r["checks"]:
        lines.append(f"  [{'ok' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
        if not c["ok"] and hint(c["detail"]):
            lines.append(f"         -> {hint(c['detail'])}")
    return "\n".join(lines)


def html_report(r: dict) -> str:
    colour = "#15803d" if r["ok"] else "#b91c1c"
    def row(c):
        extra = ""
        if not c["ok"] and hint(c["detail"]):
            extra = f"<div style='color:#b91c1c;margin-top:2px'>👉 {html.escape(hint(c['detail']))}</div>"
        return (f"<tr><td style='padding:3px 6px 3px 0;vertical-align:top'>{'✅' if c['ok'] else '❌'}</td>"
                f"<td style='padding:3px 16px 3px 0;white-space:nowrap;vertical-align:top'>{html.escape(c['name'])}</td>"
                f"<td style='padding:3px 0'>{html.escape(c['detail'])}{extra}</td></tr>")
    rows = "".join(row(c) for c in r["checks"])
    restore = f"{r['restore_seconds']:.1f}s" if r.get("restore_seconds") is not None else "n/a"
    return f"""<!doctype html><html><body style="font-family:system-ui,sans-serif;max-width:640px;margin:24px auto;color:#111">
<div style="border-left:6px solid {colour};padding:12px 16px;background:#f8fafc">
<div style="font-size:13px;color:#555">Backup Doctor · {html.escape(r['name'])} · {html.escape(r['finished_at'])}</div>
<div style="font-size:20px;font-weight:600;color:{colour};margin-top:4px">{html.escape(summary(r))}</div></div>
<table style="margin-top:16px;border-collapse:collapse;font-size:14px">
<tr><td style="padding:4px 12px 4px 0;color:#555;white-space:nowrap">Backup</td><td>{html.escape(r['backup']['source'])}</td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#555">Size</td><td>{_human_size(r['backup']['size_bytes'])}, written {r['backup']['age_hours']:.1f}h ago</td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#555;white-space:nowrap">Restore time</td><td>{restore}</td></tr></table>
<table style="margin-top:16px;border-collapse:collapse;font-size:14px;width:100%">{rows}</table>
<p style="font-size:12px;color:#777;margin-top:24px">This report contains only counts and timestamps. Backup Doctor ran inside your own environment and never copied your data anywhere.</p>
</body></html>"""


def prometheus(r: dict) -> str:
    """Metrics in Prometheus text format (for node_exporter's textfile collector or a Pushgateway)."""
    name = r["name"].replace('"', "")
    out = [
        "# HELP backup_doctor_drill_success 1 if the last restore drill passed.",
        "# TYPE backup_doctor_drill_success gauge",
        f'backup_doctor_drill_success{{drill="{name}"}} {1 if r["ok"] else 0}',
        "# HELP backup_doctor_restore_seconds Time taken to restore the backup (RTO).",
        "# TYPE backup_doctor_restore_seconds gauge",
        f'backup_doctor_restore_seconds{{drill="{name}"}} {r.get("restore_seconds") or 0:.2f}',
        "# HELP backup_doctor_backup_age_hours Age of the newest backup file.",
        "# TYPE backup_doctor_backup_age_hours gauge",
        f'backup_doctor_backup_age_hours{{drill="{name}"}} {r["backup"]["age_hours"]:.2f}',
        "# HELP backup_doctor_backup_size_bytes Size of the newest backup file.",
        "# TYPE backup_doctor_backup_size_bytes gauge",
        f'backup_doctor_backup_size_bytes{{drill="{name}"}} {r["backup"]["size_bytes"]}',
        "# HELP backup_doctor_last_drill_timestamp_seconds Unix time the last drill finished.",
        "# TYPE backup_doctor_last_drill_timestamp_seconds gauge",
        f'backup_doctor_last_drill_timestamp_seconds{{drill="{name}"}} {int(dt.datetime.now(dt.timezone.utc).timestamp())}',
    ]
    if r.get("newest_record_age_hours") is not None:
        out += [
            "# HELP backup_doctor_data_loss_window_hours Age of the newest record in the restored copy (RPO).",
            "# TYPE backup_doctor_data_loss_window_hours gauge",
            f'backup_doctor_data_loss_window_hours{{drill="{name}"}} {r["newest_record_age_hours"]:.2f}',
        ]
    return "\n".join(out) + "\n"


def write_outputs(r: dict, out_dir: str) -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    paths = {
        "json": os.path.join(out_dir, "report.json"),
        "text": os.path.join(out_dir, "report.txt"),
        "html": os.path.join(out_dir, "report.html"),
        "metrics": os.path.join(out_dir, "backup_doctor.prom"),
    }
    with open(paths["json"], "w") as f:
        json.dump(r, f, indent=2)
    with open(paths["text"], "w") as f:
        f.write(text(r) + "\n")
    with open(paths["html"], "w") as f:
        f.write(html_report(r))
    with open(paths["metrics"], "w") as f:
        f.write(prometheus(r))
    return paths


def email(r: dict, cfg: dict) -> None:
    """Send the HTML report. SMTP password comes from the SMTP_PASSWORD environment variable only."""
    if not cfg or not cfg.get("to"):
        return
    msg = EmailMessage()
    msg["Subject"] = ("✅ " if r["ok"] else "❌ ") + f"Backup drill: {r['name']}: " + ("PASS" if r["ok"] else "FAIL")
    msg["From"] = cfg.get("from", cfg.get("username", "backup-doctor@localhost"))
    msg["To"] = ", ".join(cfg["to"]) if isinstance(cfg["to"], list) else cfg["to"]
    msg.set_content(text(r))
    msg.add_alternative(html_report(r), subtype="html")
    with smtplib.SMTP(cfg.get("host", "localhost"), int(cfg.get("port", 587))) as s:
        if cfg.get("starttls", True):
            s.starttls()
        if cfg.get("username"):
            s.login(cfg["username"], os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)
