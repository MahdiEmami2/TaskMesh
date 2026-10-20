# Operations Runbook

## Processes

| Process | Command | Notes |
|---|---|---|
| API | `uvicorn taskmesh.api:app --host 0.0.0.0 --port 8000` | OpenAPI at `/docs` |
| Worker | `python -m taskmesh.worker` | scale horizontally; safe to run many |
| Scheduler | `python -m taskmesh.scheduler` | one instance is enough |
| Dashboard | any static server, e.g. `python -m http.server 8080 --directory dashboard` | talks to API at `http://localhost:8000` |

Docker Compose (`docker compose up --build`) starts all five plus PostgreSQL
and Redis. **Not verified**: no Docker daemon was available in the build
environment; validate before relying on it.

## Graceful shutdown

- Worker handles SIGTERM/SIGINT: stops claiming, drains in-flight tasks, exits.
  In-flight tasks keep their lease until the handler returns; if the process is
  killed anyway, the scheduler recovers them after `TASKMESH_LEASE_SECONDS`.
- API: stop after uvicorn finishes in-flight HTTP requests.

## Common operations

**Retry a failed task** (resets attempts and queues it):
```
curl -X POST http://localhost:8000/tasks/<id>/retry -H "Authorization: Bearer $TOKEN"
```

**Inspect a stuck task**: check `status`, `leased_by`, `lease_expires_at` via
`GET /tasks/<id>`. If `RUNNING` with an expired lease, the scheduler will
recover it within one tick.

**Purge old terminal tasks** (not automated; run manually with a DB role):
```sql
DELETE FROM task_attempts WHERE task_id IN
  (SELECT id FROM tasks WHERE status IN ('COMPLETED','FAILED','CANCELLED')
   AND finished_at < now() - interval '30 days');
DELETE FROM tasks WHERE status IN ('COMPLETED','FAILED','CANCELLED')
  AND finished_at < now() - interval '30 days';
```

## Troubleshooting

| Symptom | Likely cause / action |
|---|---|
| `/ready` returns 503 | PostgreSQL down or wrong `TASKMESH_DATABASE_URL`; check API logs |
| `active_workers: -1` in stats | Redis unreachable — heartbeats unreadable; statistic unknown, not zero |
| Tasks stuck in PENDING | no worker running, or worker can't reach the DB; check worker logs |
| Tasks stuck in RUNNING forever | worker crashed; scheduler recovers after lease expiry — is the scheduler process up? |
| Tasks stuck in SCHEDULED / RETRY_WAITING | scheduler not running |
| Duplicate execution observed | at-least-once semantics after a crash; make the handler idempotent (docs/ARCHITECTURE.md) |
| 413 on submit | payload over `TASKMESH_MAX_PAYLOAD_CHARS` |
| Migration errors | run `alembic upgrade head` with `TASKMESH_DATABASE_URL` set; never `create_all` in production |

## Monitoring hooks

- `/health` — liveness (always cheap).
- `/ready` — readiness (checks DB).
- `/stats` — counts by status, queue depth by priority, active workers.
- Loggers: `taskmesh.worker`, `taskmesh.scheduler`, `taskmesh.repository`
  emit structured one-line events with task IDs.
