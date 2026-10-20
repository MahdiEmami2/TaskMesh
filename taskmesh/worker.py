"""Worker: claims tasks from PostgreSQL and executes registered handlers.

Execution model and semantics (documented in docs/ARCHITECTURE.md):
- Claiming is atomic via SELECT ... FOR UPDATE SKIP LOCKED; a task row moves
  to RUNNING with a lease (leased_by / lease_expires_at) in the same transaction.
- Delivery is AT-LEAST-ONCE. If a worker crashes mid-execution, the lease
  expires and the task is retried — the handler may already have run once.
  Handlers must therefore be idempotent or tolerate duplicate execution.
- Cancellation is cooperative: before each execution step the worker checks
  whether the task row is still RUNNING. A cancelled task's result is discarded.
"""
from __future__ import annotations

import logging
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy.orm import Session, sessionmaker

from taskmesh import handlers as handlers_mod
from taskmesh import repository
from taskmesh.config import get_settings
from taskmesh.db import create_db_engine, make_session_factory
from taskmesh.handlers import HandlerRegistry, build_default_registry
from taskmesh.models import utcnow

logger = logging.getLogger("taskmesh.worker")

# Injected so handlers (e.g. 'flaky') can read the authoritative attempt count.
_current_attempt = threading.local()


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        registry: HandlerRegistry | None = None,
        worker_id: str | None = None,
        concurrency: int | None = None,
    ) -> None:
        settings = get_settings()
        self.session_factory = session_factory
        self.registry = registry or build_default_registry()
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.concurrency = concurrency or settings.worker_concurrency
        self.lease_seconds = settings.lease_seconds
        self.poll_interval = settings.poll_interval_seconds
        self._stop = threading.Event()
        self._executor: ThreadPoolExecutor | None = None

    # -- main loop ---------------------------------------------------------

    def run_forever(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=self.concurrency)
        stop_signal = self._install_signal_handlers()

        # Heartbeat thread keeps this worker visible in stats while idle.
        heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)
        heartbeat.start()

        logger.info("worker %s started (concurrency=%d)", self.worker_id, self.concurrency)
        while not stop_signal.is_set():
            submitted = 0
            # Fill the executor with up to `concurrency` in-flight tasks.
            while not stop_signal.is_set() and submitted < self.concurrency:
                task = self._try_claim()
                if task is None:
                    break
                self._executor.submit(self._execute_safe, task.id)
                submitted += 1
            stop_signal.wait(self.poll_interval if submitted == 0 else 0.05)
        logger.info("worker %s stopping: draining in-flight tasks", self.worker_id)
        self._executor.shutdown(wait=True)
        logger.info("worker %s stopped", self.worker_id)

    def _install_signal_handlers(self) -> threading.Event:
        stop = threading.Event()

        def _handle(signum: int, _frame: object) -> None:
            logger.info("worker %s received signal %s, initiating graceful shutdown", self.worker_id, signum)
            stop.set()

        try:
            signal.signal(signal.SIGTERM, _handle)
            signal.signal(signal.SIGINT, _handle)
        except ValueError:
            # Not in main thread (e.g. tests) — skip signal wiring.
            pass
        return stop

    def _heartbeat_loop(self) -> None:
        while True:
            try:
                self.heartbeat()
            except Exception:  # noqa: BLE001 - heartbeat must never kill the worker
                logger.exception("heartbeat failed")
            if self._stop.wait(10):
                return

    def heartbeat(self) -> dict:
        """Record worker liveness in Redis (transient; TTL-backed)."""
        import redis  # local import keeps module import light for unit tests

        client = redis.Redis.from_url(get_settings().redis_url)
        key = f"taskmesh:worker:{self.worker_id}"
        client.hset(
            key,
            mapping={
                "worker_id": self.worker_id,
                "last_seen": datetime.now(timezone.utc).isoformat(),
                "concurrency": self.concurrency,
            },
        )
        client.expire(key, 60)
        return {"worker_id": self.worker_id}

    # -- claim & execute -----------------------------------------------------

    def _try_claim(self):
        try:
            with self.session_factory() as session:
                task = repository.claim_next_task(
                    session, worker_id=self.worker_id, lease_seconds=self.lease_seconds
                )
                session.commit()
                return task
        except Exception:  # noqa: BLE001 - DB outage must not kill the loop
            logger.exception("claim failed; retrying after poll interval")
            return None

    def _execute_safe(self, task_id) -> None:
        """Reload task in a fresh session, execute handler, persist outcome.

        The DB transaction only wraps the outcome write, never the handler
        itself — long handlers must not hold a database transaction open.
        """
        try:
            with self.session_factory() as session:
                task = repository.get_task(session, task_id)
                if task is None or task.status != "RUNNING":
                    return  # cancelled / recovered by another process meanwhile
                if task.lease_expires_at and task.lease_expires_at < utcnow():
                    logger.warning("task %s lease already expired before execution", task_id)
                    return
                handler = self.registry.get(task.task_type)
                _current_attempt.value = task.attempts
                handlers_mod.set_current_attempt(task.attempts)
                started = time.monotonic()
                try:
                    result = handler.run(dict(task.payload))
                    duration_ms = int((time.monotonic() - started) * 1000)
                    session.rollback()  # end read transaction before write
                    self._persist_success(task_id, result or {"duration_ms": duration_ms})
                except BaseException as exc:  # handler failure, incl. retryable
                    duration_ms = int((time.monotonic() - started) * 1000)
                    session.rollback()
                    self._persist_failure(task_id, repository.sanitize_error(exc))
        except Exception:  # noqa: BLE001
            logger.exception("unexpected error executing task %s", task_id)

    def _persist_success(self, task_id, result: dict) -> None:
        with self.session_factory() as session:
            task = repository.get_task(session, task_id)
            if task is None or task.status != "RUNNING" or task.leased_by != self.worker_id:
                logger.info("task %s no longer owned by %s; discarding result", task_id, self.worker_id)
                return
            repository.complete_task(session, task, result)
            session.commit()
            logger.info("task %s COMPLETED (attempt %d)", task_id, task.attempts)

    def _persist_failure(self, task_id, error: str | None) -> None:
        settings = get_settings()
        with self.session_factory() as session:
            task = repository.get_task(session, task_id)
            if task is None or task.status != "RUNNING" or task.leased_by != self.worker_id:
                logger.info("task %s no longer owned by %s; discarding failure", task_id, self.worker_id)
                return
            repository.fail_task(
                session,
                task,
                error,
                backoff_base_seconds=settings.retry_backoff_base_seconds,
                backoff_max_seconds=settings.retry_backoff_max_seconds,
            )
            session.commit()
            logger.info("task %s -> %s (attempt %d/%d)", task_id, task.status, task.attempts, task.max_attempts)


def run_worker() -> None:
    """Entry point for `python -m taskmesh.worker`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    engine = create_db_engine()
    factory = make_session_factory(engine)
    Worker(session_factory=factory).run_forever()


if __name__ == "__main__":
    run_worker()
