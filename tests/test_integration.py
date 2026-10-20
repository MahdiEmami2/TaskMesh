"""Integration tests against live PostgreSQL (and Redis where noted).

These tests use real disposable services: the schema is dropped and recreated
per test via fixtures. Run with:  pytest -m integration
"""
from __future__ import annotations

import datetime as dt
import threading
import time
import uuid

import pytest

from taskmesh import repository
from taskmesh.models import Task, TaskAttempt, utcnow

pytestmark = pytest.mark.integration


@pytest.fixture()
def session(session_factory):
    with session_factory() as s:
        yield s
        s.rollback()


def mk(session, **kw) -> Task:
    defaults = dict(task_type="compute", payload={"operation": "sum", "n": 1})
    defaults.update(kw)
    t = repository.create_task(session, **defaults)
    session.commit()
    return t


class TestSubmission:
    def test_create_task_defaults(self, session):
        t = mk(session)
        assert t.status == "PENDING"
        assert t.priority == "NORMAL"
        assert t.attempts == 0

    def test_scheduled_status_for_future_run_at(self, session):
        t = mk(session, run_at=utcnow() + dt.timedelta(hours=1))
        assert t.status == "SCHEDULED"

    def test_idempotency_key_unique_constraint(self, session):
        key = f"idem-{uuid.uuid4()}"
        mk(session, idempotency_key=key)
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            mk(session, idempotency_key=key)
            session.rollback()


class TestClaiming:
    def test_claim_returns_highest_priority_first(self, session):
        low = mk(session, priority="LOW")
        high = mk(session, priority="HIGH")
        normal = mk(session, priority="NORMAL")
        w = f"w-{uuid.uuid4().hex[:6]}"
        claimed = [repository.claim_next_task(session, worker_id=w, lease_seconds=60) for _ in range(3)]
        session.commit()
        assert [t.id for t in claimed] == [high.id, normal.id, low.id]

    def test_concurrent_workers_cannot_claim_same_task(self, session, session_factory):
        """N threads race the claim query; every claimed id must be unique."""
        for _ in range(20):
            mk(session, priority="NORMAL")
        claimed_ids: list[uuid.UUID] = []
        lock = threading.Lock()
        errors: list[Exception] = []

        def worker_loop():
            try:
                with session_factory() as s:
                    t = repository.claim_next_task(
                        s, worker_id=f"w-{threading.get_ident()}", lease_seconds=60
                    )
                    s.commit()
                    if t is not None:
                        with lock:
                            claimed_ids.append(t.id)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker_loop) for _ in range(10)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert not errors
        assert len(claimed_ids) == len(set(claimed_ids)), "duplicate claim detected"

    def test_claim_does_not_take_undue_scheduled(self, session):
        future = mk(session, run_at=utcnow() + dt.timedelta(hours=1))
        w = f"w-{uuid.uuid4().hex[:6]}"
        assert repository.claim_next_task(session, worker_id=w, lease_seconds=60) is None
        session.commit()
        assert session.get(Task, future.id).status == "SCHEDULED"

    def test_claim_increments_attempts(self, session):
        t = mk(session)
        claimed = repository.claim_next_task(session, worker_id="w1", lease_seconds=60)
        session.commit()
        assert claimed.attempts == 1
        assert claimed.id == t.id

    def test_retry_waiting_claimed_only_after_next_retry_at(self, session):
        t = mk(session)
        claimed = repository.claim_next_task(session, worker_id="w1", lease_seconds=60)
        session.commit()
        repository.fail_task(
            session, claimed, "boom", backoff_base_seconds=3600, backoff_max_seconds=3600
        )
        session.commit()
        assert session.get(Task, t.id).status == "RETRY_WAITING"
        w2 = "w2"
        assert repository.claim_next_task(session, worker_id=w2, lease_seconds=60) is None
        session.commit()
        # Force due: set next_retry_at in the past.
        row = session.get(Task, t.id)
        row.next_retry_at = utcnow() - dt.timedelta(seconds=1)
        session.commit()
        again = repository.claim_next_task(session, worker_id=w2, lease_seconds=60)
        session.commit()
        assert again is not None and again.id == t.id and again.attempts == 2


class TestLifecycle:
    def test_full_success_lifecycle(self, session):
        t = mk(session)
        c = repository.claim_next_task(session, worker_id="w1", lease_seconds=60)
        repository.complete_task(session, c, {"result": 1})
        session.commit()
        row = session.get(Task, t.id)
        assert row.status == "COMPLETED"
        assert row.finished_at is not None
        attempts = session.query(TaskAttempt).filter_by(task_id=t.id).all()
        assert len(attempts) == 1 and attempts[0].status == "SUCCEEDED"
        assert attempts[0].duration_ms is not None

    def test_failure_persists_history(self, session):
        t = mk(session, max_attempts=2)
        c = repository.claim_next_task(session, worker_id="w1", lease_seconds=60)
        repository.fail_task(session, c, "first failure", backoff_base_seconds=0.01, backoff_max_seconds=0.02)
        session.commit()
        attempts = session.query(TaskAttempt).filter_by(task_id=t.id).all()
        assert len(attempts) == 1 and attempts[0].error == "first failure"
        assert session.get(Task, t.id).last_error == "first failure"

    def test_cancellation_of_pending(self, session):
        t = mk(session)
        task, changed = repository.cancel_task(session, t)
        session.commit()
        assert changed and task.status == "CANCELLED" and task.finished_at is not None

    def test_cancellation_idempotent_on_terminal(self, session):
        t = mk(session)
        repository.cancel_task(session, t)
        session.commit()
        row = session.get(Task, t.id)
        task, changed = repository.cancel_task(session, row)
        session.commit()
        assert not changed and task.status == "CANCELLED"

    def test_terminal_task_cannot_transition(self, session):
        t = mk(session)
        repository.cancel_task(session, t)
        session.commit()
        row = session.get(Task, t.id)
        with pytest.raises(repository.TransitionError):
            repository.complete_task(session, row, {})


class TestSchedulingAndRecovery:
    def test_scheduler_promotes_due_tasks(self, session):
        from taskmesh.scheduler import run_scheduler_once

        t = mk(session, run_at=utcnow() - dt.timedelta(seconds=1))
        out = run_scheduler_once(session)
        assert out["promoted"] >= 1
        assert session.get(Task, t.id).status == "PENDING"

    def test_scheduler_does_not_promote_future(self, session):
        from taskmesh.scheduler import run_scheduler_once

        t = mk(session, run_at=utcnow() + dt.timedelta(hours=1))
        run_scheduler_once(session)
        assert session.get(Task, t.id).status == "SCHEDULED"

    def test_expired_lease_recovery(self, session):
        t = mk(session, max_attempts=3)
        repository.claim_next_task(session, worker_id="dead-worker", lease_seconds=-1)
        session.commit()  # lease already expired
        assert session.get(Task, t.id).status == "RUNNING"
        from taskmesh.scheduler import run_scheduler_once

        out = run_scheduler_once(session)
        assert out["recovered"] == 1
        row = session.get(Task, t.id)
        assert row.status == "RETRY_WAITING"
        assert row.leased_by is None
        attempts = session.query(TaskAttempt).filter_by(task_id=t.id).all()
        assert attempts[0].status == "FAILED"
        assert "lease expired" in attempts[0].error

    def test_expired_lease_recovery_exhausted(self, session):
        t = mk(session, max_attempts=1)
        repository.claim_next_task(session, worker_id="dead-worker", lease_seconds=-1)
        session.commit()
        from taskmesh.scheduler import run_scheduler_once

        run_scheduler_once(session)
        assert session.get(Task, t.id).status == "FAILED"

    def test_recovery_does_not_touch_fresh_leases(self, session):
        t = mk(session)
        repository.claim_next_task(session, worker_id="live-worker", lease_seconds=120)
        session.commit()
        from taskmesh.scheduler import run_scheduler_once

        out = run_scheduler_once(session)
        assert out["recovered"] == 0
        assert session.get(Task, t.id).status == "RUNNING"


class TestPersistenceAcrossRestart:
    def test_scheduled_tasks_survive_new_session(self, session, session_factory):
        """Simulated restart: completely new session/sessionmaker sees the state."""
        run_at = utcnow() + dt.timedelta(minutes=5)
        t = mk(session, run_at=run_at)
        task_id = t.id
        session.expunge_all()  # detach everything (simulate process restart)
        with session_factory() as fresh:
            row = fresh.get(Task, task_id)
            assert row is not None
            assert row.status == "SCHEDULED"
            assert row.run_at == run_at


class TestStats:
    def test_status_counts_and_queue_depth(self, session):
        mk(session, priority="HIGH")
        mk(session)
        mk(session, priority="LOW")
        counts = repository.status_counts(session)
        assert counts["PENDING"] == 3
        depth = repository.queue_depth_by_priority(session)
        assert depth == {"HIGH": 1, "NORMAL": 1, "LOW": 1}


class TestWorkerEndToEnd:
    def test_worker_executes_task_end_to_end(self, session_factory):
        from taskmesh.worker import Worker

        with session_factory() as s:
            repository.create_task(s, task_type="compute", payload={"operation": "sum", "n": 100})
            s.commit()

        w = Worker(session_factory=session_factory, concurrency=1, worker_id="e2e-w1")
        deadline = time.time() + 15
        done = False
        while time.time() < deadline and not done:
            task = None
            with session_factory() as s:
                task = repository.claim_next_task(s, worker_id="e2e-w1", lease_seconds=60)
                s.commit()
            if task is None:
                time.sleep(0.1)
                continue
            w._execute_safe(task.id)
            with session_factory() as s:
                row = s.get(Task, task.id)
                done = row.status in ("COMPLETED", "FAILED", "CANCELLED")
        with session_factory() as s:
            rows = s.query(Task).all()
        assert len(rows) == 1
        assert rows[0].status == "COMPLETED"
        assert rows[0].result == {"result": 5050, "operation": "sum", "n": 100}

    def test_worker_retries_flaky_until_success(self, session_factory):
        from taskmesh import worker as worker_mod
        from taskmesh.worker import Worker

        with session_factory() as s:
            repository.create_task(
                s, task_type="flaky", payload={"fail_times": 1, "message": "transient"}
            )
            s.commit()

        w = Worker(session_factory=session_factory, concurrency=1, worker_id="e2e-w2")
        attempts_done = 0
        deadline = time.time() + 20
        task = None
        while time.time() < deadline and attempts_done < 3:
            with session_factory() as s:
                task = repository.claim_next_task(s, worker_id="e2e-w2", lease_seconds=60)
                s.commit()
            if task is None:
                time.sleep(0.1)
                continue
            attempts_done += 1
            worker_mod._current_attempt.value = task.attempts
            w._execute_safe(task.id)
            with session_factory() as s:
                row = s.get(Task, task.id)
                if row.status == "COMPLETED":
                    break
                if row.status == "RETRY_WAITING":
                    row.next_retry_at = utcnow() - dt.timedelta(seconds=1)
                    s.commit()
        with session_factory() as s:
            rows = s.query(Task).all()
        assert rows[0].status == "COMPLETED"
        assert rows[0].attempts == 2
        attempts = s.query(TaskAttempt).filter_by(task_id=rows[0].id).order_by(TaskAttempt.attempt_number).all()
        assert attempts[0].status == "FAILED" and attempts[1].status == "SUCCEEDED"
