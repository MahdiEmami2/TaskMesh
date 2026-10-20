"""Handler registry.

Clients never submit arbitrary code. They submit a *task type* that must be
registered here, plus a JSON payload validated by the handler's schema.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

logger = logging.getLogger("taskmesh.handlers")

Payload = dict[str, Any]


class Handler(Protocol):
    name: str

    def validate(self, payload: Payload) -> Payload: ...
    def run(self, payload: Payload) -> Payload: ...


class SchemaHandler:
    """A named handler with a Pydantic input schema and a plain function."""

    def __init__(
        self,
        name: str,
        schema: type[BaseModel],
        func: Callable[[Any], Payload],
    ) -> None:
        self.name = name
        self._schema = schema
        self._func = func

    def validate(self, payload: Payload) -> Payload:
        try:
            return self._schema.model_validate(payload).model_dump()
        except ValidationError as exc:
            raise ValueError(f"invalid payload for handler '{self.name}': {exc}") from exc

    def run(self, payload: Payload) -> Payload:
        validated = self._schema.model_validate(payload)
        return self._func(validated)


class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, handler: Handler) -> None:
        if handler.name in self._handlers:
            raise ValueError(f"handler '{handler.name}' already registered")
        self._handlers[handler.name] = handler

    def get(self, name: str) -> Handler:
        handler = self._handlers.get(name)
        if handler is None:
            raise KeyError(f"unknown task type '{name}'. Registered: {sorted(self._handlers)}")
        return handler

    def names(self) -> list[str]:
        return sorted(self._handlers)

    def validate_payload(self, name: str, payload: Payload) -> Payload:
        return self.get(name).validate(payload)


# --- Example handlers ---------------------------------------------------------


class _ComputePayload(BaseModel):
    operation: str  # 'sum' | 'factorial'
    n: int


def _compute(payload: _ComputePayload) -> Payload:
    if payload.operation == "sum":
        return {"result": sum(range(payload.n + 1)), "operation": payload.operation, "n": payload.n}
    if payload.operation == "factorial":
        result = 1
        for i in range(2, payload.n + 1):
            result *= i
        return {"result": result, "operation": payload.operation, "n": payload.n}
    raise ValueError(f"unsupported operation '{payload.operation}'")


class _LongRunningPayload(BaseModel):
    duration_seconds: float = 5.0
    steps: int = 5


def _long_running(payload: _LongRunningPayload) -> Payload:
    """Simulated long-running task. Sleeps in steps so cancellation is observable."""
    step = max(payload.duration_seconds / payload.steps, 0.01)
    slept = 0.0
    for _ in range(payload.steps):
        time.sleep(step)
        slept += step
    return {"slept_seconds": round(slept, 3), "steps": payload.steps}


class _FlakyPayload(BaseModel):
    """Fails the first N executions, then succeeds. For retry testing."""

    fail_times: int = 2
    message: str = "simulated transient failure"


class _FlakyState:
    """Process-local attempt counter, set by the worker before each execution.
    Keyed by task id so concurrent executions in one process don't collide."""

    current_attempt: dict[str, int] = {}


def _flaky(payload: _FlakyPayload) -> Payload:
    # The worker injects the authoritative DB attempt count before each run.
    attempt = _FlakyState.current_attempt.get("__current__", 0)
    if attempt <= payload.fail_times:
        raise RuntimeError(f"{payload.message} (attempt {attempt} <= fail_times {payload.fail_times})")
    return {"succeeded_after_attempts": attempt, "fail_times": payload.fail_times}


def set_current_attempt(n: int) -> None:
    """Called by the worker to expose the authoritative DB attempt count to
    handlers (e.g. 'flaky') for the current execution."""
    _FlakyState.current_attempt["__current__"] = n


def build_default_registry() -> HandlerRegistry:
    registry = HandlerRegistry()
    registry.register(SchemaHandler("compute", _ComputePayload, _compute))
    registry.register(SchemaHandler("long_running", _LongRunningPayload, _long_running))
    registry.register(SchemaHandler("flaky", _FlakyPayload, _flaky))
    return registry
