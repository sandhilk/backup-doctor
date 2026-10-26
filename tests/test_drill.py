"""End-to-end tests: real pg_dump backups restored into a real Postgres.

Needs a disposable Postgres server. Set DRILL_SERVER_URL, e.g.
    postgresql://postgres:postgres@localhost:5432/postgres
(GitHub Actions provides one as a service container.)
"""

import gzip
import json
import os
import subprocess
import uuid

import psycopg
import pytest

from backup_doctor import cli, report

SERVER = os.environ.get("DRILL_SERVER_URL")
pytestmark = pytest.mark.skipif(not SERVER, reason="DRILL_SERVER_URL not set")


def _url(db):
    base, _, _ = SERVER.rpartition("/")
    return f"{base}/{db}"


@pytest.fixture
def shop():
    """A small 'live' shop database we can back up. Returns its URL."""
    name = f"shop_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(SERVER, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    with psycopg.connect(_url(name), autocommit=True) as c:
        c.execute("CREATE TABLE customers (id serial PRIMARY KEY, email text NOT NULL)")
        c.execute("CREATE TABLE orders (id serial PRIMARY KEY, customer_id int REFERENCES customers, "
                  "total numeric(10,2), created_at timestamptz NOT NULL)")
        c.execute("INSERT INTO customers (email) SELECT 'person' || g || '@example.com' FROM generate_series(1, 200) g")
        c.execute("INSERT INTO orders (customer_id, total, created_at) "
                  "SELECT 1 + (g % 200), 24.99, now() - (g || ' minutes')::interval FROM generate_series(1, 1000) g")
    yield _url(name)
    with psycopg.connect(SERVER, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def dump(url, path, fmt="custom"):
    if fmt == "custom":
        subprocess.run(["pg_dump", "-Fc", "-f", str(path), url], check=True)
    else:
        out = subprocess.run(["pg_dump", url], check=True, capture_output=True).stdout
        with gzip.open(path, "wb") as f:
            f.write(out)
    return path


def config(backup, **extra):
    cfg = {
        "name": "test-shop",
        "backup": str(backup),
        "drill_server": SERVER,
        "checks": {
            "min_tables": 2,
            "tables": {
                "orders": {"min_rows": 100, "freshness_column": "created_at", "max_age_hours": 26},
                "customers": {"min_rows": 10},
            },
        },
    }
    cfg.update(extra)
    return cfg


def scratch_dbs():
    with psycopg.connect(SERVER) as c:
        return [r[0] for r in c.execute("SELECT datname FROM pg_database WHERE datname LIKE 'drill_%'")]


def test_good_backup_passes(shop, tmp_path):
    b = dump(shop, tmp_path / "shop.dump")
    r = cli.drill(config(b), str(tmp_path / "out"))
    assert r["ok"], report.text(r)
    assert r["row_counts"] == {"customers": 200, "orders": 1000}
    assert r["restore_seconds"] > 0
    assert r["newest_record_age_hours"] < 1


def test_plain_sql_gz_backup_passes(shop, tmp_path):
    b = dump(shop, tmp_path / "shop.sql.gz", fmt="sql.gz")
    r = cli.drill(config(b), str(tmp_path / "out"))
    assert r["ok"], report.text(r)


def test_empty_backup_fails(tmp_path):
    b = tmp_path / "shop.dump"
    b.write_bytes(b"")
    r = cli.drill(config(b), str(tmp_path / "out"))
    assert not r["ok"]
    failed = {c["name"] for c in r["checks"] if not c["ok"]}
    assert "backup is not empty" in failed


def test_corrupt_backup_fails(shop, tmp_path):
    b = dump(shop, tmp_path / "shop.dump")
    data = b.read_bytes()
    b.write_bytes(data[: len(data) // 3])  # truncated, like a backup that ran out of disk
    r = cli.drill(config(b), str(tmp_path / "out"))
    assert not r["ok"]
    assert any(c["name"] == "restore" and not c["ok"] for c in r["checks"])


def test_truncated_sql_gz_fails(shop, tmp_path):
    b = dump(shop, tmp_path / "shop.sql.gz", fmt="sql.gz")
    data = b.read_bytes()
    b.write_bytes(data[: len(data) // 2])  # gzip stream cut off half way
    r = cli.drill(config(b), str(tmp_path / "out"))
    assert not r["ok"]
    assert any(c["name"] == "restore" and "truncated" in c["detail"] for c in r["checks"])


def test_stale_data_fails_freshness(shop, tmp_path):
    with psycopg.connect(shop, autocommit=True) as c:
        c.execute("UPDATE orders SET created_at = created_at - interval '3 days'")
    b = dump(shop, tmp_path / "shop.dump")
    r = cli.drill(config(b), str(tmp_path / "out"))
    assert not r["ok"]
    assert any(c["name"] == "orders: freshness" and not c["ok"] for c in r["checks"])


def test_missing_table_fails(shop, tmp_path):
    b = dump(shop, tmp_path / "shop.dump")
    cfg = config(b)
    cfg["checks"]["tables"]["invoices"] = {"min_rows": 1}
    r = cli.drill(cfg, str(tmp_path / "out"))
    assert not r["ok"]
    assert any(c["name"] == "invoices: present" and not c["ok"] for c in r["checks"])


def test_big_row_drop_since_last_drill_fails(shop, tmp_path):
    out = str(tmp_path / "out")
    r1 = cli.drill(config(dump(shop, tmp_path / "a.dump")), out)
    assert r1["ok"]
    with psycopg.connect(shop, autocommit=True) as c:
        c.execute("DELETE FROM orders WHERE id > 300")  # someone deleted 70% of orders
    r2 = cli.drill(config(dump(shop, tmp_path / "b.dump")), out)
    assert not r2["ok"]
    assert any(c["name"] == "orders: no data loss" and not c["ok"] for c in r2["checks"])


def test_scratch_database_is_always_dropped(shop, tmp_path):
    before = set(scratch_dbs())
    cli.drill(config(dump(shop, tmp_path / "shop.dump")), str(tmp_path / "out"))
    assert set(scratch_dbs()) == before


def test_report_never_contains_customer_data(shop, tmp_path):
    out = tmp_path / "out"
    r = cli.drill(config(dump(shop, tmp_path / "shop.dump")), str(out))
    report.write_outputs(r, str(out))
    for name in ("report.json", "report.txt", "report.html", "backup_doctor.prom"):
        assert "@example.com" not in (out / name).read_text()
    assert json.loads((out / "report.json").read_text())["ok"] is True


def test_cli_exit_codes(shop, tmp_path):
    good = dump(shop, tmp_path / "shop.dump")
    cfg_file = tmp_path / "drill.yaml"
    cfg_file.write_text(json.dumps(config(good)))  # JSON is valid YAML
    assert cli.main(["drill", "-c", str(cfg_file), "--out", str(tmp_path / "o1"), "--no-email"]) == 0
    good.write_bytes(b"")
    assert cli.main(["drill", "-c", str(cfg_file), "--out", str(tmp_path / "o2"), "--no-email"]) == 1
    assert cli.main(["drill", "-c", str(tmp_path / "missing.yaml")]) == 2
