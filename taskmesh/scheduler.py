"""Scheduler: promotes due SCHEDULED / RETRY_WAITING tasks to PENDING and
recovers expired leases. Runs periodically; safe to run as its own process
(`python -m taskmesh.scheduler`) or inside the worker process.

Durability: scheduling state lives in PostgreSQL (tasks.run_at), so a
restart never loses scheduled tasks. Duplicate promotion is prevented by
the state machine — a SCHEDULED row can only become PENDING once.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy.orm import Session, sessionmaker

from taskmesh import repository
from taskmesh.config import get_settings
from taskmesh.db import create_db_engine, make_session_factory

logger = logging.getLogger("taskmesh.scheduler")


def promote_due_tasks(session: Session) -> int:
    """Move SCHEDULED tasks whose run_at is due, and RETRY_WAITING tasks whose
    next_retry_at is due, back to PENDING so workers can claim them."""
    from sqlalchemy import update

    from taskmesh.models import Task, utcnow

    now = utcnow()
    result = session.execute(
        update(Task)
        .where(
            Task.status.in_(["SCHEDULED", "RETRY_WAITING"]),
            (
                (Task.status == "SCHEDULED") & (Task.run_at <= now)
            )
            | (
                (Task.status == "RETRY_WAITING") & (Task.next_retry_at <= now)
            ),
        )
        .values(status="PENDING")
    )
    return result.rowcount or 0


def run_scheduler_once(session: Session) -> dict:
    promoted = promote_due_tasks(session)
    recovered = repository.recover_expired_leases(session)
    session.commit()
    if promoted or recovered:
        logger.info("scheduler promoted=%d recovered=%d", promoted, len(recovered))
    return {"promoted": promoted, "recovered": len(recovered)}


def run_forever(session_factory: sessionmaker[Session]) -> None:
    interval = get_settings().scheduler_interval_seconds
    logger.info("scheduler started (interval=%.1fs)", interval)
    while True:
        try:
            with session_factory() as session:
                run_scheduler_once(session)
        except Exception:  # noqa: BLE001 - scheduler must survive DB outages
            logger.exception("scheduler tick failed")
        time.sleep(interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    engine = create_db_engine()
    run_forever(make_session_factory(engine))


if __name__ == "__main__":
    main()
