"""Task state machine.

States and the legal transitions are defined explicitly here and enforced
in the persistence layer. PostgreSQL is the source of truth.
"""
from __future__ import annotations

from enum import Enum


class TaskState(str, Enum):
    PENDING = "PENDING"          # submitted, not yet scheduled/claimed
    SCHEDULED = "SCHEDULED"      # waiting for run_at (delayed/scheduled task)
    RETRY_WAITING = "RETRY_WAITING"  # failed, waiting for next retry time
    RUNNING = "RUNNING"          # claimed by a worker, inside execution lease
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"            # terminal failure (retries exhausted)
    CANCELLED = "CANCELLED"


class Priority(str, Enum):
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


PRIORITY_ORDER: dict[Priority, int] = {
    Priority.HIGH: 0,
    Priority.NORMAL: 1,
    Priority.LOW: 2,
}

# Legal transitions: from -> set of allowed to-states.
# FAILED -> PENDING is legal ONLY via the manual retry endpoint (operator action).
LEGAL_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.PENDING: {TaskState.RUNNING, TaskState.SCHEDULED, TaskState.CANCELLED},
    TaskState.SCHEDULED: {TaskState.PENDING, TaskState.RUNNING, TaskState.CANCELLED},
    TaskState.RETRY_WAITING: {TaskState.PENDING, TaskState.RUNNING, TaskState.CANCELLED},
    TaskState.RUNNING: {TaskState.COMPLETED, TaskState.FAILED, TaskState.RETRY_WAITING, TaskState.CANCELLED},
    TaskState.FAILED: {TaskState.PENDING},  # manual retry only
    # COMPLETED and CANCELLED are truly terminal.
    TaskState.COMPLETED: set(),
    TaskState.CANCELLED: set(),
}


def is_legal_transition(current: TaskState, target: TaskState) -> bool:
    return target in LEGAL_TRANSITIONS[current]
