"""Restore a backup into a THROWAWAY database, time it, and clean up afterwards.

The scratch database lives on a separate, disposable Postgres server (the "drill server"),
never on production. Each drill creates a fresh database with a unique name and drops it
when the checks are finished.
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import psycopg


class RestoreError(RuntimeError):
    pass


@dataclass
class RestoreResult:
    database_url: str   # URL of the scratch database holding the restored data
    seconds: float      # how long the restore took (this is your real-world RTO)


def _with_db(url: str, dbname: str) -> str:
    u = urlparse(url)
    return urlunparse(u._replace(path="/" + dbname))


def _detect_format(path: str) -> str:
    if path.endswith(".sql.gz"):
        return "sql.gz"
    if path.endswith(".sql"):
        return "sql"
    with open(path, "rb") as f:
        head = f.read(5)
    if head == b"PGDMP":
        return "custom"
    return "sql"


@contextmanager
def scratch_restore(backup_path: str, drill_server_url: str, keep: bool = False):
    """Restore into a new scratch database; yield the result; always drop the database afterwards."""
    name = f"drill_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    admin = psycopg.connect(drill_server_url, autocommit=True)
    try:
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
        target = _with_db(drill_server_url, name)
        start = time.monotonic()
        _restore(backup_path, target)
        result = RestoreResult(database_url=target, seconds=time.monotonic() - start)
        yield result
    finally:
        if not keep:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _restore(path: str, target_url: str) -> None:
    fmt = _detect_format(path)
    if fmt == "custom":
        cmd = ["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error", "-d", target_url, path]
        proc = subprocess.run(cmd, capture_output=True, text=True)
    else:
        cmd = ["psql", "-v", "ON_ERROR_STOP=1", "-q", "-d", target_url]
        opener = gzip.open if fmt == "sql.gz" else open
        # stream through a pipe so .sql.gz is decompressed and large files never sit in memory
        with opener(path, "rb") as src, tempfile.TemporaryFile() as err:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
            corrupt = None
            try:
                shutil.copyfileobj(src, p.stdin, 1024 * 1024)
            except BrokenPipeError:
                pass  # psql exited early; its return code and stderr tell the story
            except (EOFError, gzip.BadGzipFile, OSError) as e:
                corrupt = e  # the backup file itself is truncated or damaged
                p.kill()
            finally:
                try:
                    p.stdin.close()
                except BrokenPipeError:
                    pass
            rc = p.wait()
            if corrupt is not None:
                raise RestoreError(f"backup file is truncated or corrupt: {corrupt}")
            err.seek(0)
            proc = subprocess.CompletedProcess(cmd, rc, stderr=err.read().decode(errors="replace"))
    if proc.returncode != 0:
        tail = "\n".join(str(proc.stderr).strip().splitlines()[-5:])
        raise RestoreError(f"restore failed ({fmt} backup): {tail or 'no error output'}")


def tools_available() -> bool:
    return all(shutil.which(t) for t in ("pg_restore", "psql"))
