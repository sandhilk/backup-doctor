# Backup Doctor

**A weekly fire drill for your backups.** It restores your newest backup into a throwaway database, checks the data is really there, times the restore, and tells you in plain English whether you could actually recover.

[![ci](https://github.com/sandhilk/backup-doctor/actions/workflows/ci.yml/badge.svg)](https://github.com/sandhilk/backup-doctor/actions/workflows/ci.yml)
![license](https://img.shields.io/badge/license-MIT-blue)

| Healthy backup | Broken backup |
|---|---|
| ![pass](docs/report-pass.png) | ![fail](docs/report-fail.png) |

## Why

Most teams check that the backup job *ran*. Very few check that the backup can be *restored*.

A green "backup completed" tick only proves a file was written. It doesn't prove the file is complete, that it restores, how long the restore takes, or how much recent data it's missing. Teams usually find out on their worst day. In 2017 GitLab.com lost about six hours of production data after an accidental delete, and [their post-incident write-up](https://about.gitlab.com/2017/02/01/gitlab-dot-com-database-incident/) found that none of their five backup and replication methods were working reliably.

Backup Doctor turns "we think we have backups" into a weekly, measured answer:

- ✅ **Restores** the newest backup into a scratch database (never production)
- ✅ **Checks** tables exist, row counts look right, and nothing shrank suspiciously since last week
- ⏱️ **Measures** restore time (your real **RTO**) and the age of the newest record (your real **RPO**)
- 📨 **Reports** a one-line verdict by email, plus JSON, HTML and Prometheus metrics
- 🧹 **Cleans up** the scratch database every time, pass or fail

## How it works

```mermaid
flowchart LR
  B[(Newest backup<br/>S3 or folder)] -->|read-only| D[Backup Doctor]
  D -->|restore| S[(Scratch DB<br/>disposable Postgres)]
  D -->|count rows, newest record,<br/>compare with last week| S
  D -->|drop| S
  D --> R[Plain-English report<br/>email · HTML · JSON]
  D --> M[Prometheus metrics<br/>RTO · RPO · pass/fail]
```

## Trust model: your data never leaves your environment

| Worry | Design |
|---|---|
| "You'll see my customer data." | It runs **inside your own account**, as a Kubernetes CronJob, a container or a script. Nothing is sent anywhere else. |
| "It could break production." | It only needs **read-only** access to backups ([example IAM policy](docs/iam-read-only.json)) and restores into a **disposable** Postgres that's deleted after each drill. |
| "What's in the report?" | **Only counts and timestamps.** Never names, emails or card data. A test enforces this. |
| "Where do passwords go?" | Environment variables only, never the config file. |

## Quick start

```bash
pip install git+https://github.com/sandhilk/backup-doctor
# a disposable Postgres to restore into (never production)
docker run -d --name drill-pg -e POSTGRES_PASSWORD=drill -p 5433:5432 postgres:16
export DRILL_SERVER_URL=postgresql://postgres:drill@localhost:5433/postgres

./scripts/demo.sh          # builds a sample shop, then drills a good and a broken backup
```

Point it at your own backups:

```bash
cp examples/drill.yaml drill.yaml   # edit backup location, tables and limits
backup-doctor drill -c drill.yaml   # exit code 0 = pass, 1 = fail, 2 = config error
```

Supported today: **PostgreSQL** backups in `pg_dump` custom format (`.dump`) or plain SQL (`.sql`, `.sql.gz`), stored in a local folder or any S3-compatible bucket.

## Run it weekly on Kubernetes

[`deploy/k8s/cronjob.yaml`](deploy/k8s/cronjob.yaml) runs every Monday at 06:00 with a disposable Postgres as a native sidecar in the same pod. A failed drill means a failed Job, so the Job alerts you already have will fire. Containers run non-root with a read-only root filesystem and all capabilities dropped.

## What a drill checks

| Check | Fails when |
|---|---|
| backup is recent | the newest backup is older than `max_backup_age_hours` (default 26h) |
| backup is not empty | the file is smaller than `min_backup_bytes` |
| restore | `pg_restore` / `psql` errors, or the file is truncated or corrupt |
| restore time | the restore takes longer than `max_restore_seconds` (your RTO target) |
| tables restored | fewer than `min_tables` tables came back |
| `<table>: rows` | a key table has fewer than `min_rows` rows |
| `<table>: freshness` | the newest record is older than `max_age_hours` (your RPO) |
| `<table>: no data loss` | a table shrank more than `max_row_drop_pct` since the last passing drill |

## Tested in CI on every push

Real `pg_dump` backups restored into a real Postgres 16. Scenarios: a healthy backup, an empty file, a truncated custom dump, a truncated `.sql.gz`, stale data, a missing table, a 70% row drop since the last drill, scratch-database cleanup, and a check that reports never contain customer data. The Docker image is scanned with Trivy.

## Roadmap

- [x] Postgres: restore, row counts, freshness (RPO), restore time (RTO), drift since last drill
- [x] Email, HTML, JSON and Prometheus outputs
- [x] Kubernetes CronJob with a disposable Postgres sidecar
- [ ] MySQL / MariaDB
- [ ] Checksums on key tables
- [ ] Slack / WhatsApp alerts
- [ ] Grafana dashboard: RTO and RPO trend over time
- [ ] Optional AI explanation of failures in plain English
- [ ] Helm chart

## Built by

[Sandhil Kypa](https://www.linkedin.com/in/sandhilkypa): DevOps / Platform engineer. Started as the restore drill in [k8s-stateful-platform](https://github.com/sandhilk/k8s-stateful-platform). Built with AI-assisted engineering (Claude Code); every change is tested in CI.

MIT licence.
