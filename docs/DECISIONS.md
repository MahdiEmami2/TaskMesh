# Architecture Decision Records (condensed)

## D-1: Custom PostgreSQL-backed queue instead of Celery/RQ

**Decision.** Implement claiming with `SELECT … FOR UPDATE SKIP LOCKED` + leases
directly on PostgreSQL; use Redis only for worker heartbeats.

**Why.**
- Requirement: PostgreSQL must be the source of truth and a Redis flush must
  not lose tasks. Celery/RQ hold queue state in Redis (RQ entirely; Celery
  reliably only with a broker-based flow), which conflicts with that.
- The claim logic is ~40 lines of auditable SQL with a precise concurrency
  test, vs. reconciling two sources of truth in Celery (`taskmeta` in its own
  backend DB) or fighting RQ's Redis-only model.
- No extra broker process beyond Redis (which we already run for heartbeats).

**Trade-offs.** Polling loop (sub-second) instead of broker push — fine at
portfolio scale; no Celery features (routing, beat UI, rate limits) — not
needed here; claim ordering under concurrency is best-effort, documented.

## D-2: At-least-once delivery

Exactly-once execution cannot be guaranteed when side effects are non-DB
actions. Tasks may re-execute after a crash (lease expiry). Handlers must be
idempotent. Documented in README + ARCHITECTURE.

## D-3: Cooperative cancellation

Running Python functions cannot be killed safely in-process. Cancel marks the
row CANCELLED; the worker discards results of unowned tasks. `long_running`
sleeps in steps so cancellation is observable between steps. PENDING /
SCHEDULED / RETRY_WAITING tasks cancel immediately.

## D-4: FAILED → PENDING transition exists, but only via the retry endpoint

Keeps the state machine honest (operator-initiated manual retry is a real
flow, AC-10) while automatic flows can never resurrect a terminal task.

## D-5: Attempt rows are written at claim time

Guarantees history exists even for tasks whose worker dies before finishing —
recovery marks that row FAILED instead of leaving a gap.

## D-6: Synchronous SQLAlchemy + thread-pool workers

Handlers are CPU/IO bound functions; threads avoid asyncio-in-handler
restrictions and keep handler code trivially simple. Scale-out is horizontal
(more worker processes), not per-process async fan-out.

## D-7: Optional bearer-token auth instead of full user management

Portfolio/dev constraint: no external identity service. Token gates the
destructive endpoints when configured; deployment behind a real IdP is the
documented production path.
