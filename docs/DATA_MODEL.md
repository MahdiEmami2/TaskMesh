# Data Model

PostgreSQL is the source of truth. Schema is created exclusively by Alembic
migration `28955cf6aee0` ("create tasks and task_attempts").

## Table `tasks`

| Column | Type | Notes |
|---|---|---|
| id | UUID PK | generated server-side |
| task_type | varchar(100) NOT NULL | must match a registered handler |
| status | varchar(20) NOT NULL | CHECK: PENDING/SCHEDULED/RETRY_WAITING/RUNNING/COMPLETED/FAILED/CANCELLED |
| priority | varchar(10) NOT NULL | CHECK: HIGH/NORMAL/LOW |
| payload | JSONB NOT NULL | validated against the handler schema; size-limited |
| result | JSONB NULL | handler output of the successful attempt |
| last_error | text NULL | **sanitized**: single-line, truncated to 2000 chars, no stack trace |
| idempotency_key | varchar(200) NULL | **partial UNIQUE index** where NOT NULL |
| attempts | int NOT NULL, >= 0 | number of executions started |
| max_attempts | int NOT NULL | per-task retry budget |
| next_retry_at | timestamptz NULL | due time for RETRY_WAITING |
| run_at | timestamptz NULL | due time for SCHEDULED |
| created_at / started_at / finished_at | timestamptz | lifecycle timestamps |
| leased_by | varchar(200) NULL | worker identity while RUNNING |
| lease_expires_at | timestamptz NULL | lease expiry while RUNNING |

Indexes:
- `ux_tasks_idempotency_key` — unique partial index (idempotency enforcement at the persistence layer)
- `ix_tasks_claim (status, priority, run_at, next_retry_at)` — serves the claim query
- `ix_tasks_created_at`, `ix_tasks_task_type`, `ix_tasks_status_created` — list/filter queries

## Table `task_attempts`

| Column | Type | Notes |
|---|---|---|
| id | bigint PK | |
| task_id | UUID NOT NULL, indexed | FK by convention (no hard FK to keep attempt rows cheap) |
| attempt_number | int NOT NULL | unique per (task_id, attempt_number) |
| started_at / finished_at | timestamptz | |
| status | varchar(20) | CHECK: RUNNING/SUCCEEDED/FAILED/CANCELLED |
| error | text NULL | sanitized like `last_error` |
| duration_ms | int NULL | observable execution duration |

## Redis keys

| Key | Content | TTL |
|---|---|---|
| `taskmesh:worker:{worker_id}` | hash: worker_id, last_seen, concurrency | 60 s |

Redis holds no durable state. Flushing Redis never loses tasks, history or
scheduling data; it only makes worker counts briefly report as unavailable
(`-1` in `/stats`, distinguished from a legitimate `0`).

## Data limits

- Task payload: rejected above `TASKMESH_MAX_PAYLOAD_CHARS` (default 10 000) serialized characters.
- Errors: truncated to 2000 chars, newlines stripped, no stack traces or environment details stored or returned.
