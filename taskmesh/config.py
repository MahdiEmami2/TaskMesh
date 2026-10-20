"""Application configuration loaded from environment variables (prefix TASKMESH_)."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TASKMESH_", env_file=".env", extra="ignore")

    # Database / cache
    database_url: str = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/taskmesh"
    redis_url: str = "redis://localhost:6379/0"

    # Worker behaviour
    worker_concurrency: int = 4
    lease_seconds: int = 120  # execution lease duration; stale leases are recovered after this
    poll_interval_seconds: float = 0.5
    shutdown_grace_seconds: int = 10

    # Retry policy
    max_attempts: int = 5
    retry_backoff_base_seconds: float = 2.0
    retry_backoff_max_seconds: float = 300.0

    # API behaviour
    api_token: str | None = None  # when set, operational endpoints require Bearer token
    cors_origins: str = "http://localhost:8080"
    max_payload_chars: int = 10_000  # task payload serialized size limit
    scheduler_interval_seconds: float = 1.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
