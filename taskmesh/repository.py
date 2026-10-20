"""Task repository: all state mutations happen through this layer.

Claiming uses ``SELECT ... FOR UPDATE SKIP LOCKED`` inside a transaction so
that concurrent workers can never claim the same task. PostgreSQL is the
source of truth; Redis is only a wake-up/heartbeat aid.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from taskmesh.models import Task, TaskAttempt, utcnow
from taskmesh.states import LEGAL_TRANSITIONS, TaskState

logger = logging.getLogger("taskmesh.repository")

MAX_ERROR_CHARS = 2000


def sanitize_error(exc: BaseException | str | None, limit: int = MAX_ERROR_CHARS) -> str | None:
    """Safe error representation: truncated, single-line, no stack trace."""
    if exc is None:
        return None
    text_ = str(exc).replace("\n", " ").replace("\r", " ")
    if len(text_) > limit:
        text_ = text_[: limit - 3] + "..."
    return text_


class TransitionError(ValueError):
    """Raised when a state transition is not legal."""


def check_transition(current: str, target: str) -> None:
    cur, tgt = TaskState(current), TaskState(target)
    if tgt not in LEGAL_TRANSITIONS[cur]:
        raise TransitionError(f"Illegal state transition {cur.value} -> {tgt.value}")


def create_task(
    session: Session,
    *,
    task_type: str,
    payload: dict,
    priority: str = "NORMAL",
    run_at: dt.datetime | None = None,
    idempotency_key: str | None = None,
    max_attempts: int = 5,
) -> Task:
    task = Task(
        task_type=task_type,
        payload=payload,
        priority=priority,
        run_at=run_at,
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
        status=(TaskState.SCHEDULED.value if run_at else TaskState.PENDING.value),
    )
    session.add(task)
    session.flush()
    return task


def get_task(session: Session, task_id: uuid.UUID) -> Task | None:
    return session.get(Task, task_id)


def get_task_by_idempotency_key(session: Session, key: str) -> Task | None:
    stmt = select(Task).where(Task.idempotency_key == key)
    return session.execute(stmt).scalar_one_or_none()


def list_tasks(
    session: Session,
    *,
    status: str | None = None,
    priority: str | None = None,
    task_type: str | None = None,
    created_after: dt.datetime | None = None,
    created_before: dt.datetime | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Task], int]:
    stmt = select(Task)
    if status:
        stmt = stmt.where(Task.status == status)
    if priority:
        stmt = stmt.where(Task.priority == priority)
    if task_type:
        stmt = stmt.where(Task.task_type == task_type)
    if created_after:
        stmt = stmt.where(Task.created_at >= created_after)
    if created_before:
        stmt = stmt.where(Task.created_at <= created_before)
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(stmt.order_by(Task.created_at.desc()).limit(limit).offset(offset)).scalars().all()
    return list(rows), int(total)


# --- Claiming (atomic, race-free) -------------------------------------------

_CLAIM_SQL = text("""
    WITH candidate AS (
        SELECT id FROM tasks
        WHERE (
            (status = 'PENDING')
            OR (status = 'RETRY_WAITING' AND next_retry_at <= :now)
            OR (status = 'SCHEDULED' AND run_at <= :now)
        )
        ORDER BY
            CASE priority WHEN 'HIGH' THEN 0 WHEN 'NORMAL' THEN 1 ELSE 2 END ASC,
            created_at ASC
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    UPDATE tasks t
    SET status = 'RUNNING',
        started_at = COALESCE(t.started_at, :now),
        leased_by = :worker_id,
        lease_expires_at = :lease_expires,
        attempts = t.attempts + 1
    FROM candidate
    WHERE t.id = candidate.id
    RETURNING t.id
""")


def claim_next_task(session: Session, *, worker_id: str, lease_seconds: int) -> Task | None:
    """Atomically claim one eligible task. Priority then FIFO ordering.

    ``FOR UPDATE SKIP LOCKED`` guarantees that two concurrent workers can
    never claim the same row; the loser simply skips to another candidate.
    """
    now = utcnow()
    lease_expires = now + dt.timedelta(seconds=lease_seconds)
    task_id = session.execute(
        _CLAIM_SQL,
        {"now": now, "worker_id": worker_id, "lease_expires": lease_expires},
    ).scalar()
    if task_id is None:
        return None
    # The raw UPDATE bypassed the ORM identity map — refresh cached rows so
    # callers observe the post-claim state (RUNNING, attempts+1).
    session.expire_all()
    task = session.get(Task, task_id)
    if task is None:  # defensive: row vanished inside the same transaction
        return None
    session.add(
        TaskAttempt(
            task_id=task_id,
            attempt_number=task.attempts,
            started_at=now,
            status="RUNNING",
        )
    )
    session.flush()
    return task


# --- Completion / failure ----------------------------------------------------


def mark_attempt_finished(
    session: Session | None, task: Task, *, status: str, error: str | None = None
) -> None:
    if session is None:
        return  # pure-logic callers (unit tests) skip attempt persistence
    stmt = (
        select(TaskAttempt)
        .where(TaskAttempt.task_id == task.id, TaskAttempt.attempt_number == task.attempts)
        .with_for_update()
    )
    attempt = session.execute(stmt).scalar_one_or_none()
    if attempt is None:
        logger.warning("attempt row missing for task %s attempt %s", task.id, task.attempts)
        return
    attempt.status = status
    attempt.finished_at = utcnow()
    attempt.duration_ms = int((attempt.finished_at - attempt.started_at).total_seconds() * 1000)
    attempt.error = error


def complete_task(session: Session | None, task: Task, result: dict) -> Task:
    check_transition(task.status, TaskState.COMPLETED.value)
    task.status = TaskState.COMPLETED.value
    task.result = result
    task.finished_at = utcnow()
    task.leased_by = None
    task.lease_expires_at = None
    task.next_retry_at = None
    mark_attempt_finished(session, task, status="SUCCEEDED")
    return task


def fail_task(
    session: Session | None,
    task: Task,
    error: str | None,
    *,
    backoff_base_seconds: float,
    backoff_max_seconds: float,
) -> Task:
    """Fail the current attempt: retry with exponential backoff or go terminal."""
    mark_attempt_finished(session, task, status="FAILED", error=error)

    if task.attempts < task.max_attempts:
        # Exponential backoff: base ** attempts, capped at the configured max.
        delay = min(backoff_base_seconds * (2 ** (task.attempts - 1)), backoff_max_seconds)
        check_transition(task.status, TaskState.RETRY_WAITING.value)
        task.status = TaskState.RETRY_WAITING.value
        task.next_retry_at = utcnow() + dt.timedelta(seconds=delay)
        task.last_error = error
        task.leased_by = None
        task.lease_expires_at = None
    else:
        check_transition(task.status, TaskState.FAILED.value)
        task.status = TaskState.FAILED.value
        task.last_error = error
        task.finished_at = utcnow()
        task.leased_by = None
        task.lease_expires_at = None
    return task


# --- Cancellation ------------------------------------------------------------


def cancel_task(session: Session, task: Task) -> tuple[Task, bool]:
    """Request cancellation. Returns (task, was_cancelled_now).

    PENDING / SCHEDULED / RETRY_WAITING tasks are cancelled immediately.
    RUNNING tasks get a cooperative flag; the running worker observes it.
    Terminal tasks are returned unchanged (already final).
    """
    if task.status in (TaskState.COMPLETED.value, TaskState.FAILED.value, TaskState.CANCELLED.value):
        return task, False
    if task.status == TaskState.RUNNING.value:
        mark_attempt_finished(session, task, status="CANCELLED", error="cancellation requested")
        task.leased_by = None
        task.lease_expires_at = None
        task.status = TaskState.CANCELLED.value
        task.finished_at = utcnow()
        return task, True
    check_transition(task.status, TaskState.CANCELLED.value)
    task.status = TaskState.CANCELLED.value
    task.finished_at = utcnow()
    return task, True


# --- Lease recovery (worker crash) ------------------------------------------


def recover_expired_leases(session: Session) -> list[Task]:
    """Recover tasks whose execution lease expired (worker crash).

    The attempt is marked FAILED (a real outcome was never observed: the
    task may have executed already — at-least-once semantics) and the task
    is moved to RETRY_WAITING if attempts remain, else FAILED.
    """
    now = utcnow()
    stmt = (
        select(Task)
        .where(
            Task.status == TaskState.RUNNING.value,
            Task.lease_expires_at.is_not(None),
            Task.lease_expires_at <= now,
        )
        .with_for_update(skip_locked=True)
    )
    expired = list(session.execute(stmt).scalars().all())
    for task in expired:
        logger.warning("recovering task %s from expired lease held by %s", task.id, task.leased_by)
        mark_attempt_finished(
            session, task, status="FAILED", error=f"lease expired (worker {task.leased_by} likely crashed)"
        )
        task.leased_by = None
        task.lease_expires_at = None
        if task.attempts < task.max_attempts:
            task.status = TaskState.RETRY_WAITING.value
            task.next_retry_at = now
            task.last_error = "lease expired; recovered for retry"
        else:
            task.status = TaskState.FAILED.value
            task.last_error = "lease expired; retries exhausted"
            task.finished_at = now
    return expired


# --- Statistics --------------------------------------------------------------


def status_counts(session: Session) -> dict[str, int]:
    rows = session.execute(select(Task.status, func.count()).group_by(Task.status)).all()
    counts = {s.value: 0 for s in TaskState}
    for status, count in rows:
        counts[status] = count
    return counts


def queue_depth_by_priority(session: Session) -> dict[str, int]:
    """Queue depth = PENDING + RETRY_WAITING + due-SCHEDULED counts by priority."""
    rows = session.execute(
        select(Task.priority, func.count())
        .where(
            Task.status.in_(
                [TaskState.PENDING.value, TaskState.RETRY_WAITING.value, TaskState.SCHEDULED.value]
            )
        )
        .group_by(Task.priority)
    ).all()
    depth = {"HIGH": 0, "NORMAL": 0, "LOW": 0}
    for priority, count in rows:
        depth[priority] = count
    return depth
