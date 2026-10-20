"""SQLAlchemy 2.x ORM models for TaskMesh.

PostgreSQL is the source of truth for task metadata, status and execution
history. Redis holds only transient coordination data (wake-up signals,
worker heartbeats) and may be flushed without losing task data.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from taskmesh.states import TaskState

TASK_STATES = [s.value for s in TaskState]
PRIORITIES = ["HIGH", "NORMAL", "LOW"]


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=TaskState.PENDING.value)
    priority: Mapped[str] = mapped_column(String(10), nullable=False, default="NORMAL")

    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)  # truncated, no secrets

    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    next_retry_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Execution lease: worker identity + expiry. NULL when not running.
    leased_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('PENDING','SCHEDULED','RETRY_WAITING','RUNNING','COMPLETED','FAILED','CANCELLED')", name="ck_tasks_status"),
        CheckConstraint("priority IN ('HIGH','NORMAL','LOW')", name="ck_tasks_priority"),
        CheckConstraint("attempts >= 0", name="ck_tasks_attempts_nonneg"),
        # Idempotency: same key must never create two logical tasks.
        Index("ux_tasks_idempotency_key", "idempotency_key", unique=True, postgresql_where=text("idempotency_key IS NOT NULL")),
        # Claiming query: eligible (PENDING / due retry / due scheduled) by priority.
        Index("ix_tasks_claim", "status", "priority", "run_at", "next_retry_at"),
        Index("ix_tasks_created_at", "created_at"),
        Index("ix_tasks_task_type", "task_type"),
        Index("ix_tasks_status_created", "status", "created_at"),
    )


class TaskAttempt(Base):
    __tablename__ = "task_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # RUNNING / SUCCEEDED / FAILED / CANCELLED
    error: Mapped[str | None] = mapped_column(Text, nullable=True)  # truncated
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('RUNNING','SUCCEEDED','FAILED','CANCELLED')", name="ck_attempts_status"),
        Index("ux_attempts_task_attempt", "task_id", "attempt_number", unique=True),
    )
