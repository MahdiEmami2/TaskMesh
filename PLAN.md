# TaskMesh — Implementation Plan

## Status: IMPLEMENTATION COMPLETE (core) — see checkpoint

## Decisions

- **Execution architecture**: custom PostgreSQL-backed worker instead of Celery/RQ.
  Rationale: Celery/RQ treat Redis as the source of truth for task state, which
  conflicts with the requirement that PostgreSQL hold status/history and that a
  Redis flush must not lose tasks. A Postgres claim loop (`FOR UPDATE SKIP LOCKED`
  + leases) keeps one source of truth, is simple to audit for race conditions and
  is fully testable against a disposable database. Redis is used only for worker
  heartbeats (TTL keys) — transient coordination, never durable state.
- **Delivery semantics**: at-least-once. Duplicate execution after a worker crash
  is possible and documented; handlers should be idempotent.
- **Scheduling**: `run_at` stored in PostgreSQL; scheduler process promotes due
  SCHEDULED/RETRY_WAITING rows to PENDING. Restarts lose nothing.
- **API auth**: optional bearer token via `TASKMESH_API_TOKEN` (dev-friendly, no
  secrets required); destructive endpoints are gated when configured.

## Work breakdown

1. [x] Environment inspection (Python 3.12.3, no git repo, no docker; PostgreSQL 16 + Redis installed locally via apt)
2. [x] Project scaffolding, pinned deps (pyproject.toml)
3. [x] State machine (states.py), legal transitions
4. [x] Models + Alembic migration (28955cf6aee0) applied to live DB
5. [x] Repository layer: atomic claim, retry/backoff, cancel, lease recovery, stats
6. [x] Handler registry + 3 example handlers (compute, long_running, flaky)
7. [x] Worker (thread pool, graceful shutdown, heartbeats, lease-aware execution)
8. [x] Scheduler process (promotion + lease recovery)
9. [x] FastAPI app (submit, list/filter/paginate, detail, attempts, cancel, retry, stats, health/ready)
10. [ ] Dashboard (static HTML+JS against real API)
11. [ ] Tests (unit + integration with real PostgreSQL/Redis)
12. [x] Lint (ruff), types (mypy) — both pass clean
13. [x] Docs (README with German summary, all 8 docs)
14. [x] Docker Compose + GitHub Actions (written; **not verified** — no Docker daemon in sandbox)
15. [x] Self-review against risk checklist (race conditions, ownership guard, no eval/exec, error surfaces)
16. [x] Release packaging dist/TaskMesh.zip + validation (unzip -t, content audit)

## Checkpoint (updated as work proceeds)

- Implemented: items 1–16 (complete).
- Test totals: 60 passed (19 unit + 24 integration + 17 API), ruff and mypy clean.
- Resume point: none — see completion report in the conversation.
