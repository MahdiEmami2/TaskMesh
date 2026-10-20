# Acceptance Criteria

Each criterion is verified by an explicit test or command; the verification
result is recorded in docs/TESTING.md and the completion report.

## API
- AC-1: `POST /tasks` with a registered task type and valid payload returns 201 and the created task with status PENDING (or SCHEDULED when `run_at` is in the future).
- AC-2: `POST /tasks` with an unknown task type returns 422 with a structured error.
- AC-3: `POST /tasks` with a payload violating the handler schema returns 422.
- AC-4: `POST /tasks` with a payload exceeding the size limit returns 413.
- AC-5: Two submissions with the same idempotency key return the same task id; the second response has `created: false`. This also holds for concurrent duplicate submissions (unique index proof).
- AC-6: `GET /tasks` supports `status`, `priority`, `task_type`, `created_after`/`created_before`, `limit`, `offset` and returns `total`.
- AC-7: `GET /tasks/{id}` returns 404 for unknown ids.
- AC-8: `GET /tasks/{id}/attempts` returns persisted attempt history with error details and durations.
- AC-9: `POST /tasks/{id}/cancel` cancels PENDING/SCHEDULED/RETRY_WAITING tasks; terminal tasks return the current state unchanged (409-free idempotent behavior documented); RUNNING tasks transition to CANCELLED (cooperative model) and the worker discards the orphaned result.
- AC-10: `POST /tasks/{id}/retry` on a FAILED or RETRY_WAITING task resets it to PENDING; other states return 409.
- AC-11: `GET /stats` returns counts by status, queue depth by priority and worker count; worker unavailability is reported as `-1` (unknown), distinguished from `0`.
- AC-12: `/health` returns 200; `/ready` returns 503 when the database is unreachable.

## Lifecycle
- AC-13: All state changes go through the transition table; illegal transitions raise `TransitionError` (HTTP 409 where surfaced).
- AC-14: Terminal states (COMPLETED, FAILED, CANCELLED) have no outgoing transitions.
- AC-15: Tasks record created/started/finished timestamps, attempt count, next retry time and sanitized last error.

## Execution
- AC-16: Priority ordering is HIGH > NORMAL > LOW, FIFO within a priority, verified by claim-order tests.
- AC-17: Concurrent workers cannot claim the same task (verified with parallel claim attempts against the live DB).
- AC-18: A failing task retries with exponential backoff (2^n, capped) until `max_attempts`, then becomes FAILED. Attempt history shows each failure.
- AC-19: Cancelled RUNNING tasks do not have their handler result persisted.

## Scheduling / recovery
- AC-20: A task with future `run_at` is SCHEDULED, is not claimed early, and becomes claimable after `run_at` (scheduler promotion).
- AC-21: A RUNNING task with an expired lease is recovered by the scheduler: attempt marked FAILED, task moves to RETRY_WAITING (or FAILED when attempts exhausted).
- AC-22: Scheduled/retry metadata survives a restart (persisted in PostgreSQL — verified by dropping in-process state and re-reading).

## Persistence
- AC-23: Schema is created by Alembic migration `28955cf6aee0` (not by create_all) on a clean database.
- AC-24: Idempotency key uniqueness is enforced by a partial unique index at the database level.

## Quality
- AC-25: `ruff check` and `mypy` pass with the project configuration.
- AC-26: The pytest suite passes; integration tests run against real disposable PostgreSQL and Redis.
- AC-27: The dashboard renders data exclusively from the live API and shows explicit loading/empty/error states.
- AC-28: `dist/TaskMesh.zip` contains source, docs, migrations, tests, Docker and CI files, and excludes `.git`, caches, venvs and secrets; archive integrity verified with `unzip -t`.
