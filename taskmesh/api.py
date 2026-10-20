"""TaskMesh REST API (FastAPI)."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from taskmesh import repository
from taskmesh.config import get_settings
from taskmesh.db import create_db_engine, make_session_factory
from taskmesh.handlers import build_default_registry
from taskmesh.models import Task, TaskAttempt
from taskmesh.states import TaskState

app = FastAPI(
    title="TaskMesh API",
    version="0.1.0",
    description="Background task execution and monitoring system.",
)
_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _settings.cors_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

engine = create_db_engine()
SessionFactory = make_session_factory(engine)
registry = build_default_registry()


def get_db() -> Any:
    with SessionFactory() as session:
        yield session


def require_token(authorization: str | None = Header(default=None)) -> None:
    """Protect operational endpoints only when TASKMESH_API_TOKEN is set."""
    if not _settings.api_token:
        return
    if authorization != f"Bearer {_settings.api_token}":
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


# --- Schemas -------------------------------------------------------------------


class SubmitTaskRequest(BaseModel):
    task_type: str = Field(min_length=1, max_length=100, examples=["compute"])
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: str = Field(default="NORMAL", pattern="^(HIGH|NORMAL|LOW)$")
    run_at: dt.datetime | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)
    max_attempts: int = Field(default=5, ge=1, le=50)


class TaskOut(BaseModel):
    id: uuid.UUID
    task_type: str
    status: str
    priority: str
    payload: dict[str, Any]
    result: dict[str, Any] | None
    last_error: str | None
    attempts: int
    max_attempts: int
    next_retry_at: dt.datetime | None
    run_at: dt.datetime | None
    created_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    leased_by: str | None
    lease_expires_at: dt.datetime | None

    class Config:
        from_attributes = True


class AttemptOut(BaseModel):
    id: int
    task_id: uuid.UUID
    attempt_number: int
    started_at: dt.datetime
    finished_at: dt.datetime | None
    status: str
    error: str | None
    duration_ms: int | None

    class Config:
        from_attributes = True


class TaskPage(BaseModel):
    items: list[TaskOut]
    total: int
    limit: int
    offset: int


class SubmitResponse(BaseModel):
    task: TaskOut
    created: bool  # False when an idempotent replay returned the existing task


class StatsResponse(BaseModel):
    task_counts_by_status: dict[str, int]
    queue_depth_by_priority: dict[str, int]
    active_workers: int
    registered_task_types: list[str]


# --- Error handling --------------------------------------------------------------


@app.exception_handler(repository.TransitionError)
async def transition_error_handler(_req: Request, exc: repository.TransitionError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "state_transition", "detail": str(exc)})


@app.exception_handler(Exception)
async def unhandled_error_handler(_req: Request, exc: Exception) -> JSONResponse:
    # Never leak internal exception details to clients.
    return JSONResponse(status_code=500, content={"error": "internal_error", "detail": "unexpected server error"})


# --- Endpoints --------------------------------------------------------------------


@app.get("/health", tags=["ops"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready", tags=["ops"])
def ready(db: Session = Depends(get_db)) -> Any:
    try:
        db.execute(select(func.count()).select_from(Task))
        return {"status": "ready", "database": "ok"}
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"status": "not_ready", "database": "unavailable"})


@app.post("/tasks", response_model=SubmitResponse, status_code=status.HTTP_201_CREATED, tags=["tasks"])
def submit_task(req: SubmitTaskRequest, db: Session = Depends(get_db)) -> Any:
    # Payload size guard (documented limit; also protects JSONB storage).
    import json

    if len(json.dumps(req.payload)) > _settings.max_payload_chars:
        raise HTTPException(413, detail=f"payload exceeds {_settings.max_payload_chars} serialized chars")
    try:
        registry.validate_payload(req.task_type, req.payload)
    except KeyError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc

    if req.idempotency_key:
        existing = repository.get_task_by_idempotency_key(db, req.idempotency_key)
        if existing:
            return SubmitResponse(task=TaskOut.model_validate(existing), created=False)
    try:
        task = repository.create_task(
            db,
            task_type=req.task_type,
            payload=req.payload,
            priority=req.priority,
            run_at=req.run_at,
            idempotency_key=req.idempotency_key,
            max_attempts=req.max_attempts,
        )
        db.commit()
    except Exception as exc:  # unique violation on idempotency key = concurrent duplicate
        db.rollback()
        if req.idempotency_key:
            existing = repository.get_task_by_idempotency_key(db, req.idempotency_key)
            if existing:
                return SubmitResponse(task=TaskOut.model_validate(existing), created=False)
        raise HTTPException(500, detail="failed to persist task") from exc
    return SubmitResponse(task=TaskOut.model_validate(task), created=True)


@app.get("/tasks", response_model=TaskPage, tags=["tasks"])
def list_tasks(
    status_filter: str | None = Query(default=None, alias="status"),
    priority: str | None = Query(default=None, pattern="^(HIGH|NORMAL|LOW)$"),
    task_type: str | None = None,
    created_after: dt.datetime | None = None,
    created_before: dt.datetime | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> TaskPage:
    if status_filter and status_filter not in [s.value for s in TaskState]:
        raise HTTPException(422, detail=f"unknown status '{status_filter}'")
    tasks, total = repository.list_tasks(
        db,
        status=status_filter,
        priority=priority,
        task_type=task_type,
        created_after=created_after,
        created_before=created_before,
        limit=limit,
        offset=offset,
    )
    return TaskPage(
        items=[TaskOut.model_validate(t) for t in tasks], total=total, limit=limit, offset=offset
    )


@app.get("/tasks/{task_id}", response_model=TaskOut, tags=["tasks"])
def get_task(task_id: uuid.UUID, db: Session = Depends(get_db)) -> Any:
    task = repository.get_task(db, task_id)
    if task is None:
        raise HTTPException(404, detail="task not found")
    return task


@app.get("/tasks/{task_id}/attempts", response_model=list[AttemptOut], tags=["tasks"])
def get_attempts(task_id: uuid.UUID, db: Session = Depends(get_db)) -> Any:
    if repository.get_task(db, task_id) is None:
        raise HTTPException(404, detail="task not found")
    rows = db.execute(
        select(TaskAttempt)
        .where(TaskAttempt.task_id == task_id)
        .order_by(TaskAttempt.attempt_number)
    ).scalars().all()
    return [AttemptOut.model_validate(a) for a in rows]


@app.post("/tasks/{task_id}/cancel", response_model=TaskOut, tags=["tasks"])
def cancel_task(task_id: uuid.UUID, db: Session = Depends(get_db), _t: None = Depends(require_token)) -> Any:
    task = repository.get_task(db, task_id)
    if task is None:
        raise HTTPException(404, detail="task not found")
    task, _cancelled = repository.cancel_task(db, task)
    db.commit()
    return task


@app.post("/tasks/{task_id}/retry", response_model=TaskOut, tags=["tasks"])
def retry_task(task_id: uuid.UUID, db: Session = Depends(get_db), _t: None = Depends(require_token)) -> Any:
    """Retry an eligible task. Eligible: terminal FAILED tasks, or RETRY_WAITING
    tasks to force an immediate retry (next_retry_at cleared)."""
    task = repository.get_task(db, task_id)
    if task is None:
        raise HTTPException(404, detail="task not found")
    if task.status not in ("FAILED", "RETRY_WAITING"):
        raise HTTPException(
            409,
            detail=f"task is {task.status}; only FAILED or RETRY_WAITING tasks can be retried",
        )
    repository.check_transition(task.status, "PENDING")
    task.status = "PENDING"
    task.next_retry_at = None
    task.finished_at = None
    task.attempts = 0
    db.commit()
    return task


@app.get("/stats", response_model=StatsResponse, tags=["ops"])
def stats(db: Session = Depends(get_db)) -> StatsResponse:
    import redis as redis_lib

    active_workers = 0
    try:
        client = redis_lib.Redis.from_url(_settings.redis_url)
        active_workers = len([k for k in client.scan_iter("taskmesh:worker:*")])
    except Exception:  # noqa: BLE001 - report as unavailable, not zero-cause
        active_workers = -1  # -1 signals "unknown/unavailable" per docs

    return StatsResponse(
        task_counts_by_status=repository.status_counts(db),
        queue_depth_by_priority=repository.queue_depth_by_priority(db),
        active_workers=active_workers,
        registered_task_types=registry.names(),
    )
