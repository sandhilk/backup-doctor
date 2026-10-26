"""Find and fetch the newest backup file.

Supported locations:
  - a local file:            /backups/shop.dump
  - a local folder (newest): /backups/
  - an S3 prefix (newest):   s3://my-bucket/postgres/

Backup Doctor only ever READS the backup. It never writes to or deletes from the source.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

BACKUP_SUFFIXES = (".dump", ".backup", ".sql", ".sql.gz")


@dataclass
class Backup:
    path: Path              # local path to the file we will restore
    source: str             # where it came from (shown in the report)
    size_bytes: int
    created_at: dt.datetime  # when the backup file was written (UTC)

    @property
    def age_hours(self) -> float:
        return (dt.datetime.now(dt.timezone.utc) - self.created_at).total_seconds() / 3600


def _is_backup(name: str) -> bool:
    return name.endswith(BACKUP_SUFFIXES)


def fetch(location: str, workdir: str | None = None) -> Backup:
    if location.startswith("s3://"):
        return _fetch_s3(location, workdir or tempfile.mkdtemp(prefix="backup-doctor-"))
    return _fetch_local(Path(location))


def _fetch_local(p: Path) -> Backup:
    if p.is_dir():
        files = [f for f in p.iterdir() if f.is_file() and _is_backup(f.name)]
        if not files:
            raise FileNotFoundError(f"no backup files ({', '.join(BACKUP_SUFFIXES)}) in {p}")
        p = max(files, key=lambda f: f.stat().st_mtime)
    if not p.exists():
        raise FileNotFoundError(f"backup not found: {p}")
    st = p.stat()
    return Backup(
        path=p,
        source=str(p),
        size_bytes=st.st_size,
        created_at=dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc),
    )


def _fetch_s3(url: str, workdir: str) -> Backup:
    import boto3  # imported lazily so local-only users don't need it

    bucket, _, prefix = url[len("s3://"):].partition("/")
    kwargs = {}
    if os.environ.get("AWS_ENDPOINT_URL"):  # MinIO / SeaweedFS / any S3-compatible store
        kwargs["endpoint_url"] = os.environ["AWS_ENDPOINT_URL"]
    s3 = boto3.client("s3", **kwargs)

    newest = None
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if _is_backup(obj["Key"]) and (newest is None or obj["LastModified"] > newest["LastModified"]):
                newest = obj
    if newest is None:
        raise FileNotFoundError(f"no backup files under {url}")

    local = Path(workdir) / Path(newest["Key"]).name
    s3.download_file(bucket, newest["Key"], str(local))
    return Backup(
        path=local,
        source=f"s3://{bucket}/{newest['Key']}",
        size_bytes=newest["Size"],
        created_at=newest["LastModified"].astimezone(dt.timezone.utc),
    )
