"""Shared pytest fixtures.

Integration fixtures require live PostgreSQL + Redis (env vars below) and are
marked `integration`. Unit tests run anywhere.
"""
from __future__ import annotations

import os

import pytest

from taskmesh.db import create_db_engine

TEST_DB_URL = os.environ.get(
    "TASKMESH_TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/taskmesh",
)
TEST_REDIS_URL = os.environ.get("TASKMESH_TEST_REDIS_URL", "redis://localhost:6379/1")


def _services_up() -> bool:
    try:
        from sqlalchemy import text

        eng = create_db_engine(TEST_DB_URL)
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        import redis

        redis.Redis.from_url(TEST_REDIS_URL).ping()
        return True
    except Exception:  # noqa: BLE001
        return False


SERVICES_AVAILABLE = _services_up()


@pytest.fixture()
def db_engine():
    if not SERVICES_AVAILABLE:
        pytest.skip("live PostgreSQL/Redis not available; integration test skipped (NOT EXECUTED)")
    eng = create_db_engine(TEST_DB_URL)
    yield eng
    eng.dispose()


@pytest.fixture()
def session_factory(db_engine):
    from alembic import config as alembic_config
    from taskmesh.db import make_session_factory

    # Schema is built by Alembic migrations, never create_all, so tests
    # exercise the exact DDL that production uses.
    cfg = alembic_config.Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    from alembic import command

    with db_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as conn:
        conn.exec_driver_sql(
            "DROP TABLE IF EXISTS task_attempts, tasks, alembic_version CASCADE"
        )
    command.upgrade(cfg, "head")
    return make_session_factory(db_engine)
