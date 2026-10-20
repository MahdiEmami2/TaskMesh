<a id="readme-top"></a>

<div align="center">

<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/taskmesh-wordmark-light.svg">
  <img src="assets/taskmesh-wordmark-dark.svg" alt="TaskMesh — Background task execution and monitoring" width="610">
</picture>

**Durable background tasks with retries, leases and crash recovery — on PostgreSQL, observed live.**

[Architecture](#architecture) · [Features](#main-features) · [Install](#installation-and-startup-local-without-docker) · [API](#api-examples-verified-against-the-implemented-api) · [Dashboard](#dashboard) · [Testing](#testing) · [Limitations](#known-limitations) · [Deutsch](#deutsche-projektzusammenfassung-german-summary)

</div>

# TaskMesh

A background task execution and monitoring system for long-running, scheduled,
prioritized, and failure-prone workloads — built with FastAPI, PostgreSQL,
Redis and SQLAlchemy 2.x.

TaskMesh keeps long-running operations from blocking HTTP requests. Work is
submitted to the API, persisted in PostgreSQL, executed by independent worker
processes, and observed through a REST API and a live dashboard: attempt
history, retries with exponential backoff, cancellation, scheduling, and
crash recovery.

![Python](https://img.shields.io/badge/python-3.10%2B-3776ab?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169e1?logo=postgresql&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-7.x-dc382d?logo=redis&logoColor=white)
![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.x-d71f00)
![Docker Compose](https://img.shields.io/badge/Docker-Compose-2496ed?logo=docker&logoColor=white)
![ruff](https://img.shields.io/badge/lint-ruff-bf4b00)
![mypy](https://img.shields.io/badge/types-mypy-1f5082)
![License: MIT](https://img.shields.io/badge/license-MIT-2e7d32)

## At a glance

| | |
| --- | --- |
| ⚡ **Non-blocking** | The request returns immediately with a task ID; the work runs on workers. |
| 🗄️ **One source of truth** | PostgreSQL holds all task state; Redis only holds transient heartbeats. |
| 🔁 **Retries with backoff** | Exponential backoff, configurable base, cap and attempt budget. |
| 🧾 **Full attempt history** | Every attempt is recorded and visible through the API and dashboard. |
| 🛟 **Crash recovery** | Lease expiry lets the scheduler recover tasks from dead workers. |
| 🪪 **Idempotent submission** | Client-supplied keys, enforced by a partial unique index. |
| ⏰ **Scheduling and priorities** | `run_at` tasks survive restarts; HIGH > NORMAL > LOW. |
| 📺 **Live dashboard** | Plain HTML/JS, no build step, reading only from the real API. |

> [!IMPORTANT]
> **Delivery is at-least-once.** After a worker crash a task runs again, so handlers must be idempotent. See [Delivering semantics](#delivering-semantics--summary-read-this-before-relying-on-taskmesh).

<details>
<summary><strong>Table of contents</strong></summary>

1. [Why this exists](#why-this-exists-real-problem-statement)
2. [Architecture](#architecture)
3. [Main features](#main-features)
4. [Technology stack](#technology-stack)
5. [Design decisions](#design-decisions)
6. [Prerequisites](#prerequisites)
7. [Installation and startup](#installation-and-startup-local-without-docker)
8. [Environment configuration](#environment-configuration)
9. [Database migrations](#database-migrations)
10. [API examples](#api-examples-verified-against-the-implemented-api)
11. [Dashboard](#dashboard)
12. [Running workers and scheduled tasks](#running-workers-and-scheduled-tasks)
13. [Testing](#testing)
14. [Troubleshooting](#troubleshooting)
15. [Security notes](#security-notes)
16. [Known limitations](#known-limitations)
17. [Roadmap](#roadmap)
18. [Delivering semantics](#delivering-semantics--summary-read-this-before-relying-on-taskmesh)
19. [Deutsche Projektzusammenfassung](#deutsche-projektzusammenfassung-german-summary)
20. [License](#license)

</details>

## Why this exists (real problem statement)

Web handlers that run heavy work inline block requests, time out, and lose
all trace when the process restarts. TaskMesh moves such work into durable
background tasks: the request returns immediately with a task ID, the work
runs on workers with retry budgets and leases, and every attempt is recorded.
If a worker crashes, the task is recovered and retried automatically.

## Architecture

```mermaid
flowchart LR
    C[Client / Dashboard] -->|REST / JSON| API[FastAPI :8000]
    API -->|insert task row| PG[(PostgreSQL\nsource of truth)]
    subgraph Worker process
        W1[Worker loop\nconcurrency N] -->|claim:\nFOR UPDATE SKIP LOCKED\n+ lease| PG
        W1 -->|execute registered handler| H[Handler registry\ncompute / long_running / flaky]
        W1 -->|heartbeat TTL key| RD[(Redis)]
    end
    S[Scheduler loop\npromote due rows,\nrecover expired leases] --> PG
    API -->|stats: heartbeat scan| RD
```

```mermaid
stateDiagram-v2
    [*] --> PENDING
    [*] --> SCHEDULED: run_at in future
    SCHEDULED --> PENDING: scheduler promotes when due
    PENDING --> RUNNING: worker claims (lease)
    SCHEDULED --> RUNNING: worker claims
    RETRY_WAITING --> PENDING: scheduler promotes when due
    RUNNING --> COMPLETED
    RUNNING --> FAILED: attempts exhausted
    RUNNING --> RETRY_WAITING: attempt failed, retries remain
    RUNNING --> CANCELLED: cancel requested; worker discards result
    PENDING --> CANCELLED
    SCHEDULED --> CANCELLED
    RETRY_WAITING --> CANCELLED
    FAILED --> PENDING: manual retry endpoint only
    COMPLETED --> [*]
    CANCELLED --> [*]
```

## Main features

- REST API: submit (with priority, scheduled time, idempotency key), list with
  filters + pagination, detail, attempt history, cancel, retry, stats,
  health/readiness — all documented via OpenAPI at `/docs`.
- Deliberate execution architecture: custom PostgreSQL-backed queue with
  `FOR UPDATE SKIP LOCKED` claims and leases (see docs/DECISIONS.md for the
  comparison with Celery/RQ).
- State machine with enforced legal transitions; PostgreSQL is the source of
  truth; Redis holds only transient worker heartbeats.
- Priorities HIGH > NORMAL > LOW (FIFO within priority), configurable worker
  concurrency.
- Retries with exponential backoff (configurable base, cap, max attempts) and
  full attempt history.
- Idempotent submission via client-supplied key, enforced by a partial unique
  index.
- Scheduled tasks (`run_at`) persisted in PostgreSQL — restart-safe.
- Worker crash recovery via lease expiry.
- Live dashboard (plain HTML/JS) reading exclusively from the real API.
- Alembic migrations, Docker Compose, GitHub Actions CI, ruff + mypy clean.

## Technology stack

| Component | Version (pinned) |
|---|---|
| Python | 3.10+ (developed and tested on 3.12.3) |
| FastAPI / Uvicorn | 0.115.6 / 0.34.0 |
| SQLAlchemy / Alembic | 2.0.36 / 1.14.0 |
| psycopg (Postgres driver) | 3.2.3 |
| redis-py | 5.2.1 |
| PostgreSQL | 16 (tested) |
| Redis | 7.x (tested) |
| pytest / ruff / mypy | 8.3.4 / 0.8.6 / 1.14.1 |
| Docker Compose / GitHub Actions | compose file provided, CI workflow provided |

## Design decisions

See `docs/DECISIONS.md` for the full records. The headline decisions:

- **Custom PostgreSQL-backed queue** instead of Celery/RQ so that task state
  has exactly one source of truth and a Redis flush is lossless.
- **At-least-once delivery.** A task that was executing when a worker crashed
  will run again. Handlers must be idempotent or tolerate duplicates.
  Exactly-once execution is NOT guaranteed (that is impossible with
  non-transactional side effects).
- **Cooperative cancellation.** Running Python functions are not force-killed;
  the worker discards results of cancelled tasks. Queued tasks cancel
  immediately.

## Prerequisites

- Python 3.10+
- PostgreSQL 14+ and Redis 6+ (local services or Docker)
- No paid APIs, no external services, no secret credentials required

## Installation and startup (local, without Docker)

```powershell
# 1. Clone / copy the project
cd taskmesh

# 2. Virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 3. Configure environment
copy .env.example .env
#   edit .env if your PostgreSQL/Redis differ from the defaults

# 4. Create the schema (Alembic migration)
$env:TASKMESH_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/taskmesh"
alembic upgrade head

# 5. Start the three processes (each in its own terminal)
uvicorn taskmesh.api:app --host 0.0.0.0 --port 8000     # API
python -m taskmesh.worker                                # worker
python -m taskmesh.scheduler                             # scheduler

# 6. Dashboard (third terminal)
python -m http.server 8080 --directory dashboard
#   open http://localhost:8080
```

**One command with Docker Compose** (API, worker, scheduler, PostgreSQL,
Redis, dashboard):

```powershell
docker compose up --build
# API http://localhost:8000  •  Dashboard http://localhost:8080
```

> ⚠️ The Docker Compose setup was **written but not executed** in the build
> environment (no Docker daemon available). Validate it before relying on it.

## Environment configuration

All settings are environment variables with the `TASKMESH_` prefix (see
`.env.example`). The important ones:

| Variable | Default | Meaning |
|---|---|---|
| `TASKMESH_DATABASE_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/taskmesh` | PostgreSQL connection |
| `TASKMESH_REDIS_URL` | `redis://localhost:6379/0` | Redis (heartbeats only) |
| `TASKMESH_WORKER_CONCURRENCY` | `4` | threads per worker |
| `TASKMESH_LEASE_SECONDS` | `120` | execution lease duration |
| `TASKMESH_MAX_ATTEMPTS` | `5` | default retry budget |
| `TASKMESH_RETRY_BACKOFF_BASE_SECONDS` | `2.0` | backoff base (2^n) |
| `TASKMESH_RETRY_BACKOFF_MAX_SECONDS` | `300.0` | backoff cap |
| `TASKMESH_API_TOKEN` | *(empty)* | when set, cancel/retry require Bearer auth |
| `TASKMESH_CORS_ORIGINS` | `http://localhost:8080` | comma-separated allow-list |
| `TASKMESH_MAX_PAYLOAD_CHARS` | `10000` | payload size limit (413 beyond) |

## Database migrations

```powershell
$env:TASKMESH_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/taskmesh"
alembic upgrade head                                   # apply
alembic revision --autogenerate -m "your change"       # create new migration
alembic downgrade -1                                   # roll back one
```

## API examples (verified against the implemented API)

```powershell
# Submit a computation task
curl -X POST http://localhost:8000/tasks -H "Content-Type: application/json" ^
  -d '{"task_type":"compute","payload":{"operation":"sum","n":1000}}'

# Submit with high priority + idempotency key
curl -X POST http://localhost:8000/tasks -H "Content-Type: application/json" ^
  -d '{"task_type":"long_running","payload":{"duration_seconds":10},
       "priority":"HIGH","idempotency_key":"report-2026-10"}'

# Schedule a task for later
curl -X POST http://localhost:8000/tasks -H "Content-Type: application/json" ^
  -d '{"task_type":"compute","payload":{"operation":"factorial","n":20},
       "run_at":"2026-12-31T12:00:00Z"}'

# List failed tasks
curl "http://localhost:8000/tasks?status=FAILED&limit=10"

# Task detail + attempt history
curl http://localhost:8000/tasks/<id>
curl http://localhost:8000/tasks/<id>/attempts

# Cancel / retry
curl -X POST http://localhost:8000/tasks/<id>/cancel
curl -X POST http://localhost:8000/tasks/<id>/retry

# Queue + worker statistics
curl http://localhost:8000/stats
```

Expected real response (from an actual run during development):

```json
{"task":{"id":"7ef41944-8b0e-4a37-a0e4-5661bbb9a8b7","task_type":"compute",
 "status":"PENDING","payload":{"operation":"sum","n":1000},...},"created":true}
```

```json
{"task_counts_by_status":{"COMPLETED":2},"queue_depth_by_priority":{"HIGH":0,"NORMAL":0,"LOW":0},
 "active_workers":1,"registered_task_types":["compute","flaky","long_running"]}
```

## Dashboard

Plain HTML/JS served from `dashboard/` — no build step, no framework. It
renders **only** data from the real API (counts by status, queue depth,
workers, error summaries, task table with filters/pagination, detail dialog
with attempt history, submit/cancel/retry actions). Loading, empty, error and
API-unavailable states are explicit. Start it with:

```powershell
python -m http.server 8080 --directory dashboard
```

The dashboard calls `http://localhost:8000` by default; override with
`window.TASKMESH_API_URL` before the script or by editing `API` in
`dashboard/index.html`.

## Running workers and scheduled tasks

- Workers scale horizontally: start several `python -m taskmesh.worker`
  processes; the `SKIP LOCKED` claim makes double-claims impossible.
- The scheduler (`python -m taskmesh.scheduler`) promotes due scheduled /
  retry-waiting tasks and recovers expired leases. Run one instance.
- Graceful shutdown: SIGTERM/SIGINT drains in-flight tasks; a hard kill is
  still safe — leases expire and the scheduler recovers the tasks.

## Testing

```powershell
# Full suite (needs local PostgreSQL + Redis)
$env:TASKMESH_TEST_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/taskmesh"
$env:TASKMESH_TEST_REDIS_URL = "redis://localhost:6379/1"
python -m pytest tests -v

# Only service-free unit tests
python -m pytest -m "not integration" -v

# Lint + types
ruff check taskmesh tests
mypy taskmesh
```

See `docs/TESTING.md` for what is covered and the recorded results.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/ready` → 503 | PostgreSQL down / wrong URL; check API logs |
| `active_workers: -1` | Redis unreachable — statistic unknown, not zero |
| Tasks stuck PENDING | start a worker; check its DB connection |
| Tasks stuck RUNNING | worker crashed — ensure scheduler runs; leases recover |
| 413 on submit | payload over `TASKMESH_MAX_PAYLOAD_CHARS` |
| Password auth failed | set the correct `TASKMESH_DATABASE_URL`; check `pg_hba.conf` |

## Security notes

See `docs/SECURITY.md` (checklist included). Highlights: no arbitrary code
execution from payloads (registry + schema validation), no internal error
leakage, optional bearer token for operational endpoints, narrow CORS, no
secrets in the repository, payload size limits.

## Known limitations

- **At-least-once delivery** — duplicate execution possible after crashes;
  handlers must be idempotent.
- Priority ordering is best-effort under high concurrency (a worker may claim
  a lower-priority task inserted a moment before a higher one).
- Cooperative cancellation cannot abort a handler mid-CPU-bound-operation.
- No built-in authentication for general API access (only optional token on
  cancel/retry); deploy behind an authenticating proxy for multi-user use.
- No automatic purging of old terminal tasks (runbook SQL provided).
- Single PostgreSQL instance; no HA story.
- Docker Compose and CI written but **not verified** in the build
  environment.

## Roadmap

1. Handler-level cancellation tokens + subprocess isolation for hard kills.
2. Prometheus `/metrics` endpoint.
3. Rate limiting and full authentication story.
4. Scheduled-task crons (recurring schedules, not just one-shot `run_at`).
5. Task result retention policies with automatic pruning.
6. Dead-letter queue view with bulk requeue in the dashboard.

## Delivering semantics — summary (read this before relying on TaskMesh)

- Submission is **deduplicated** when an idempotency key is supplied
  (DB-enforced).
- Execution is **at-least-once**: after a worker crash the task re-runs and
  the previous (unobserved) execution may have completed its side effects.
- Design handlers to be idempotent; the framework cannot do this for you.

---

## Deutsche Projektzusammenfassung (German summary)

**TaskMesh** ist ein System zur Ausführung und Überwachung von Hintergrundaufgaben
für langlebende, geplante, priorisierte und fehleranfällige Workloads. Das Projekt
entstand als Portfolio-Arbeit im Rahmen einer IT-Ausbildung und demonstriert
praxisnah: REST-API-Entwicklung mit FastAPI, relationale Datenmodellierung mit
PostgreSQL und SQLAlchemy 2.x, Migrationen mit Alembic, Nebenläufigkeit und
Synchronisation (Row-Level-Locking mit `FOR UPDATE SKIP LOCKED` und
Eigentums-Leases), Wiederholungsstrategien mit exponentiellem Backoff,
Idempotenz auf Persistenzebene, kooperatives Abbrechen, Crash-Recovery durch
ablaufende Leases, strukturiertes Logging, automatisierte Tests (pytest, echte
Integrations- gegen PostgreSQL und Redis), statische Analysen (ruff, mypy),
Docker-Compose-Bereitstellung und CI mit GitHub Actions. Die Dokumentation
(Architektur, Datenmodell, Sicherheit, Betrieb, Tests, Entscheidungen) folgt
professionellen Standards. Die Kernentscheidung — eine eigene, PostgreSQL-basierte
Warteschlange statt Celery/RQ — ist ausführlich begründet: PostgreSQL bleibt die
alleinige Quelle der Wahrheit; Redis dient nur für vergängliche Herzschlag-Signale.
Das System liefert „At-Least-Once“-Ausführung; die Dokumentation erklärt offen die
Risiken doppelter Ausführung und deren Behandlung durch idempotente Handler.

---

## License

MIT (portfolio project).

<p align="right"><a href="#readme-top">↑ back to top</a></p>
