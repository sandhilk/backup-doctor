"""Command line entry point.

    backup-doctor drill --config drill.yaml

Exit codes: 0 = backup passed the drill, 1 = backup failed the drill, 2 = configuration error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import yaml

from . import __version__, checks, report, restore, sources


def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    # allow ${ENV_VAR} in the config so secrets never live in the file
    def expand(v):
        if isinstance(v, str):
            return os.path.expandvars(v)
        if isinstance(v, dict):
            return {k: expand(x) for k, x in v.items()}
        if isinstance(v, list):
            return [expand(x) for x in v]
        return v
    cfg = expand(cfg)
    for key in ("backup", "drill_server"):
        if not cfg.get(key):
            raise ValueError(f"config is missing '{key}'")
    return cfg


def drill(cfg: dict, out_dir: str, keep: bool = False) -> dict:
    name = cfg.get("name", "default")
    state_file = os.path.join(out_dir, "baseline.json")
    baseline = None
    if os.path.exists(state_file):
        with open(state_file) as f:
            baseline = json.load(f)

    result: dict = {"name": name, "ok": False, "checks": [], "restore_seconds": None,
                    "newest_record_age_hours": None, "version": __version__}

    b = sources.fetch(cfg["backup"])
    result["backup"] = {"source": b.source, "size_bytes": b.size_bytes, "age_hours": b.age_hours,
                        "created_at": b.created_at.isoformat()}

    pre = []
    max_backup_age = cfg.get("max_backup_age_hours", 26)
    pre.append({"name": "backup is recent", "ok": b.age_hours <= max_backup_age,
                "detail": f"written {b.age_hours:.1f}h ago (limit {max_backup_age}h)"})
    min_size = cfg.get("min_backup_bytes", 1024)
    pre.append({"name": "backup is not empty", "ok": b.size_bytes >= min_size,
                "detail": f"{b.size_bytes:,} bytes (expected at least {min_size:,})"})
    result["checks"].extend(pre)

    try:
        with restore.scratch_restore(str(b.path), cfg["drill_server"], keep=keep) as r:
            result["restore_seconds"] = r.seconds
            max_restore = cfg.get("max_restore_seconds")
            if max_restore:
                result["checks"].append({"name": "restore time", "ok": r.seconds <= max_restore,
                                         "detail": f"{r.seconds:.1f}s (target {max_restore}s)"})
            else:
                result["checks"].append({"name": "restore", "ok": True, "detail": f"completed in {r.seconds:.1f}s"})
            f = checks.run(r.database_url, cfg.get("checks", {}), baseline)
    except restore.RestoreError as e:
        result["checks"].append({"name": "restore", "ok": False, "detail": str(e)})
        f = None

    if f:
        result["checks"].extend({"name": c.name, "ok": c.ok, "detail": c.detail} for c in f.checks)
        result["row_counts"] = f.row_counts
        result["newest_record_age_hours"] = f.newest_record_age_hours

    result["ok"] = all(c["ok"] for c in result["checks"])
    result["finished_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    os.makedirs(out_dir, exist_ok=True)
    if f and result["ok"]:  # only a passing drill becomes the new baseline
        with open(state_file, "w") as fh:
            json.dump(f.row_counts, fh, indent=2)
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="backup-doctor", description="A fire drill for your backups.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("drill", help="restore the newest backup into a scratch database and check it")
    d.add_argument("--config", "-c", required=True)
    d.add_argument("--out", default=os.environ.get("BACKUP_DOCTOR_OUT", "./drill-results"))
    d.add_argument("--keep", action="store_true", help="keep the scratch database for debugging")
    d.add_argument("--no-email", action="store_true")
    args = p.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if not restore.tools_available():
        print("pg_restore and psql must be installed", file=sys.stderr)
        return 2

    try:
        result = drill(cfg, args.out, keep=args.keep)
    except FileNotFoundError as e:
        result = {"name": cfg.get("name", "default"), "ok": False, "restore_seconds": None,
                  "newest_record_age_hours": None, "version": __version__,
                  "backup": {"source": cfg["backup"], "size_bytes": 0, "age_hours": 0, "created_at": None},
                  "checks": [{"name": "backup found", "ok": False, "detail": str(e)}],
                  "finished_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}

    report.write_outputs(result, args.out)
    print(report.text(result))
    if not args.no_email:
        try:
            report.email(result, cfg.get("email") or {})
        except OSError as e:
            print(f"warning: could not send email: {e}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
