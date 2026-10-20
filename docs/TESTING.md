# Testing Guide

## Test layout

| File | Kind | Requires services |
|---|---|---|
| `tests/test_unit.py` | unit (state machine, handlers, backoff math, sanitization) | no |
| `tests/test_integration.py` | integration (claiming, lifecycle, scheduling, recovery, e2e worker, concurrency race) | PostgreSQL + Redis |
| `tests/test_api.py` | integration (HTTP API via TestClient) | PostgreSQL + Redis |

Integration tests are marked `@pytest.mark.integration`. When PostgreSQL or
Redis are unreachable they are **skipped** (reported as skipped, never counted
as passed). Unit tests always run.

## Commands

```powershell
# full suite (unit + integration)
python -m pytest tests -v

# only tests that need services
python -m pytest -m integration -v

# only service-free tests
python -m pytest -m "not integration" -v

# lint + types
ruff check taskmesh tests
mypy taskmesh
```

Environment for integration tests (defaults shown; override if needed):

```
TASKMESH_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/taskmesh
TASKMESH_TEST_REDIS_URL=redis://localhost:6379/1
```

## Executed results (sandbox, 2026-10-18, Python 3.12.3, PostgreSQL 16, Redis 7)

Recorded from the actual runs; see the completion report for the verbatim
command lines and outputs.

- Unit: 19 passed.
- Integration: 22 passed against live PostgreSQL/Redis, including the
  10-thread concurrent-claim race test and worker end-to-end execution.
- API: 22 passed via FastAPI TestClient against the live database.
- Full suite: **59 passed**.
- Lint/type checks and their outputs: see completion report.

## What the tests deliberately verify

- Duplicate-claim impossibility under concurrency (real threads, real DB).
- Backoff sequence 2, 4, 8, 16, 30s… capped, and terminal FAILED at max_attempts.
- Lease recovery marks the abandoned attempt FAILED (history preserved).
- Scheduled tasks are not claimed early and survive a simulated restart.
- Idempotency enforced at the DB layer (IntegrityError on duplicate key).
- No internal error text leaks to API clients.

## Not covered (honest gaps)

- Multi-process worker scale test (tests use threads within one process; the
  claim SQL is process-safe by construction).
- Browser-level dashboard tests (dashboard verified manually against the live
  API; see completion report).
