"""Unit tests: state machine, handlers, retry math, sanitization. No services required."""
from __future__ import annotations

import pytest

from taskmesh import repository
from taskmesh.handlers import build_default_registry
from taskmesh.states import LEGAL_TRANSITIONS, TaskState, is_legal_transition

# --- State machine -----------------------------------------------------------


class TestStateMachine:
    def test_all_legal_transitions_are_well_formed(self):
        for state in TaskState:
            for target in LEGAL_TRANSITIONS[state]:
                assert is_legal_transition(state, target)

    def test_truly_terminal_states_have_no_outgoing_transitions(self):
        for terminal in (TaskState.COMPLETED, TaskState.CANCELLED):
            assert LEGAL_TRANSITIONS[terminal] == set()

    def test_failed_to_pending_legal_only_via_manual_retry(self):
        assert is_legal_transition(TaskState.FAILED, TaskState.PENDING)

    def test_illegal_transition_completed_to_running(self):
        assert not is_legal_transition(TaskState.COMPLETED, TaskState.RUNNING)

    def test_illegal_transition_cancelled_to_pending(self):
        assert not is_legal_transition(TaskState.CANCELLED, TaskState.PENDING)

    def test_illegal_transition_failed_to_completed(self):
        assert not is_legal_transition(TaskState.FAILED, TaskState.COMPLETED)

    def test_check_transition_raises(self):
        with pytest.raises(repository.TransitionError):
            repository.check_transition("COMPLETED", "RUNNING")

    def test_required_states_exist(self):
        required = {"PENDING", "RUNNING", "FAILED", "COMPLETED", "CANCELLED"}
        assert required <= {s.value for s in TaskState}


# --- Handlers -----------------------------------------------------------------


class TestHandlers:
    def test_registry_has_example_handlers(self):
        names = build_default_registry().names()
        assert {"compute", "long_running", "flaky"} <= set(names)

    def test_compute_sum(self):
        h = build_default_registry().get("compute")
        assert h.run({"operation": "sum", "n": 10})["result"] == 55

    def test_compute_factorial(self):
        h = build_default_registry().get("compute")
        assert h.run({"operation": "factorial", "n": 5})["result"] == 120

    def test_compute_rejects_unknown_operation(self):
        h = build_default_registry().get("compute")
        with pytest.raises(ValueError, match="unsupported operation"):
            h.run({"operation": "rm -rf", "n": 1})

    def test_compute_rejects_wrong_types(self):
        h = build_default_registry().get("compute")
        with pytest.raises(ValueError, match="invalid payload"):
            h.validate({"operation": "sum", "n": "not-a-number"})

    def test_unknown_handler_raises_keyerror(self):
        with pytest.raises(KeyError):
            build_default_registry().get("does_not_exist")

    def test_no_arbitrary_code_execution(self):
        """Payloads are data, not code: there is no eval/exec anywhere."""
        import inspect

        import taskmesh.handlers as handlers_mod

        src = inspect.getsource(handlers_mod)
        assert "eval(" not in src and "exec(" not in src


# --- Error sanitization ---------------------------------------------------------


class TestSanitizeError:
    def test_none_stays_none(self):
        assert repository.sanitize_error(None) is None

    def test_newlines_stripped(self):
        out = repository.sanitize_error("line1\nline2\rsecret")
        assert "\n" not in out and "\r" not in out

    def test_truncated(self):
        out = repository.sanitize_error("x" * 5000)
        assert len(out) == 2000 and out.endswith("...")


# --- Retry backoff math (pure logic via fail_task on a stub task) ----------------


class TestRetryBackoff:
    def _make_task(self, attempts: int, max_attempts: int):
        from taskmesh.models import Task

        return Task(
            id=__import__("uuid").uuid4(),
            task_type="compute",
            status="RUNNING",
            priority="NORMAL",
            payload={},
            attempts=attempts,
            max_attempts=max_attempts,
        )

    def test_backoff_doubles_and_caps(self, monkeypatch):
        import datetime as dt

        import taskmesh.repository as repo

        frozen = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        monkeypatch.setattr(repo, "utcnow", lambda: frozen)

        delays = []
        for attempt in range(1, 7):
            task = self._make_task(attempt, max_attempts=10)
            task.created_at = frozen
            repo.fail_task(
                session=None,  # type: ignore[arg-type]
                task=task,
                error="boom",
                backoff_base_seconds=2.0,
                backoff_max_seconds=30.0,
            )
            delays.append((task.next_retry_at - task.created_at).total_seconds())
            assert task.status == "RETRY_WAITING"
        # attempts 1..6 give 2,4,8,16,30,30 (capped at max)
        assert delays == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]

    def test_retries_exhausted_becomes_failed(self):
        task = self._make_task(attempts=5, max_attempts=5)
        repository.fail_task(
            session=None,  # type: ignore[arg-type]
            task=task,
            error="boom",
            backoff_base_seconds=2.0,
            backoff_max_seconds=30.0,
        )
        assert task.status == "FAILED"
        assert task.finished_at is not None
