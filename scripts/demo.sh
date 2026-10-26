#!/usr/bin/env bash
# Demo: build a small shop database, back it up, then run two drills:
#   1. a healthy backup     -> PASS report
#   2. a broken backup      -> FAIL report (the kind you only find out about on your worst day)
# Needs DRILL_SERVER_URL pointing at a disposable Postgres server.
set -euo pipefail

: "${DRILL_SERVER_URL:?set DRILL_SERVER_URL to a disposable Postgres server}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${WORK:-$ROOT/demo-results}"
rm -rf "$WORK" && mkdir -p "$WORK/backups-good" "$WORK/backups-broken"

BASE="${DRILL_SERVER_URL%/*}"
SHOP="$BASE/demo_shop"

psql -q "$DRILL_SERVER_URL" -c "DROP DATABASE IF EXISTS demo_shop WITH (FORCE)" -c "CREATE DATABASE demo_shop"
psql -q "$SHOP" <<'SQL'
CREATE TABLE customers (id serial PRIMARY KEY, email text NOT NULL);
CREATE TABLE products  (sku text PRIMARY KEY, name text, price numeric(10,2), stock int);
CREATE TABLE orders    (id serial PRIMARY KEY, customer_id int REFERENCES customers, total numeric(10,2), created_at timestamptz NOT NULL);
INSERT INTO customers (email) SELECT 'customer' || g || '@example.com' FROM generate_series(1, 2500) g;
INSERT INTO products VALUES ('SNV-TEE-001','Origin Tee',24.99,50),('SNV-TEE-003','Grind Oversized',29.99,40),('SNV-TEE-009','Forge Heavyweight',34.99,25);
INSERT INTO orders (customer_id, total, created_at)
  SELECT 1 + (g % 2500), 24.99 + (g % 3) * 5, now() - (g || ' minutes')::interval FROM generate_series(1, 20000) g;
SQL

pg_dump -Fc -f "$WORK/backups-good/shop-$(date +%Y%m%d).dump" "$SHOP"
# a "successful" nightly job whose disk filled up: the file exists, the job went green, the data is gone
head -c 4096 "$WORK/backups-good/"*.dump > "$WORK/backups-broken/shop-$(date +%Y%m%d).dump"

for kind in good broken; do
  cat > "$WORK/drill-$kind.yaml" <<YAML
name: demo-shop-$kind
backup: $WORK/backups-$kind/
drill_server: $DRILL_SERVER_URL
max_restore_seconds: 300
checks:
  min_tables: 3
  tables:
    orders:    { min_rows: 1000, freshness_column: created_at, max_age_hours: 26 }
    customers: { min_rows: 100 }
    products:  { min_rows: 1 }
YAML
  set +e
  backup-doctor drill -c "$WORK/drill-$kind.yaml" --out "$WORK/$kind" --no-email
  rc=$?
  set -e
  echo "exit code ($kind): $rc"
  echo
done

psql -q "$DRILL_SERVER_URL" -c "DROP DATABASE IF EXISTS demo_shop WITH (FORCE)"
# the good drill must pass and the broken one must fail, otherwise the demo itself is broken
python3 - "$WORK" <<'PY'
import json, sys
w = sys.argv[1]
good = json.load(open(f"{w}/good/report.json"))["ok"]
broken = json.load(open(f"{w}/broken/report.json"))["ok"]
assert good and not broken, (good, broken)
print("demo OK: healthy backup passed, broken backup was caught")
PY
