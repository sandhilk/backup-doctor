"""Checks run against the RESTORED copy.

Reports only numbers and timestamps (row counts, newest record age). It never reads or
outputs customer data such as names, emails or card details.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import psycopg
from psycopg import sql


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Findings:
    checks: list[Check] = field(default_factory=list)
    row_counts: dict[str, int] = field(default_factory=dict)
    newest_record_age_hours: float | None = None  # the data-loss window (RPO) if you restored now

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(name, ok, detail))


def _s(v) -> str:
    # SQL_ASCII databases hand back bytes; normalise to str
    return v.decode() if isinstance(v, (bytes, bytearray)) else v


def row_counts(conn: psycopg.Connection) -> dict[str, int]:
    tables = conn.execute(
        "SELECT schemaname::text, tablename::text FROM pg_tables "
        "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2"
    ).fetchall()
    counts = {}
    for schema, table in tables:
        schema, table = _s(schema), _s(table)
        q = sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table))
        key = table if schema == "public" else f"{schema}.{table}"
        counts[key] = conn.execute(q).fetchone()[0]
    return counts


def newest(conn: psycopg.Connection, table: str, column: str) -> dt.datetime | None:
    schema, _, name = table.rpartition(".")
    ident = sql.Identifier(schema or "public", name)
    q = sql.SQL("SELECT max({}) FROM {}").format(sql.Identifier(column), ident)
    value = conn.execute(q).fetchone()[0]
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value


def run(database_url: str, cfg: dict, baseline: dict[str, int] | None) -> Findings:
    f = Findings()
    with psycopg.connect(database_url) as conn:
        f.row_counts = row_counts(conn)

        min_tables = cfg.get("min_tables", 1)
        f.add("tables restored", len(f.row_counts) >= min_tables,
              f"{len(f.row_counts)} tables (expected at least {min_tables})")

        for table, rules in (cfg.get("tables") or {}).items():
            if table not in f.row_counts:
                f.add(f"{table}: present", False, "table missing from the restored backup")
                continue
            rows = f.row_counts[table]
            min_rows = rules.get("min_rows", 1)
            f.add(f"{table}: rows", rows >= min_rows, f"{rows:,} rows (expected at least {min_rows:,})")

            col = rules.get("freshness_column")
            if col:
                ts = newest(conn, table, col)
                if ts is None:
                    f.add(f"{table}: freshness", False, f"no values in {col}")
                else:
                    age = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 3600
                    f.newest_record_age_hours = age if f.newest_record_age_hours is None else min(f.newest_record_age_hours, age)
                    limit = rules.get("max_age_hours", 26)
                    f.add(f"{table}: freshness", age <= limit,
                          f"newest record is {age:.1f}h old (limit {limit}h)")

        max_drop = cfg.get("max_row_drop_pct", 10)
        if baseline:
            for table, before in baseline.items():
                now = f.row_counts.get(table, 0)
                if before > 0 and now < before * (1 - max_drop / 100):
                    f.add(f"{table}: no data loss", False,
                          f"rows fell from {before:,} to {now:,} since the last drill (> {max_drop}% drop)")
    return f
