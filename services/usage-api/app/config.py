"""Runtime configuration, read from environment variables.

Everything is configurable through the environment so the same image runs in
docker-compose, EKS dev and EKS prod without a rebuild.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_thresholds(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        values = tuple(sorted({int(p) for p in raw.split(",") if p.strip()}))
    except ValueError:
        return default
    return values or default


@dataclass(frozen=True)
class Settings:
    service_name: str = "usage-api"
    environment: str = "dev"
    log_level: str = "INFO"

    # Data store. SQLite is only for local runs and tests; use PostgreSQL
    # (RDS/Aurora) everywhere else, e.g. postgresql+psycopg://user:pw@host/db
    database_url: str = "sqlite+pysqlite:///./usage.db"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    auto_migrate: bool = True
    seed_subscribers: int = 25

    # Event bus. When SQS_QUEUE_URL is empty the API still works; alert events
    # are logged and dropped (handy for running the API on its own).
    aws_region: str = "us-east-1"
    sqs_queue_url: str = ""
    sqs_endpoint_url: str | None = None
    alert_thresholds: tuple[int, ...] = (80, 100)

    # Failure injection ("chaos") used by the Week 4 failure scenarios.
    chaos_enabled: bool = False
    chaos_latency_ms: int = 0
    chaos_error_rate: float = 0.0

    otel_enabled: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            service_name=os.getenv("SERVICE_NAME", "usage-api"),
            environment=os.getenv("ENVIRONMENT", "dev"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            database_url=os.getenv("DATABASE_URL", cls.database_url),
            db_pool_size=_env_int("DB_POOL_SIZE", cls.db_pool_size),
            db_max_overflow=_env_int("DB_MAX_OVERFLOW", cls.db_max_overflow),
            auto_migrate=_env_bool("AUTO_MIGRATE", True),
            seed_subscribers=_env_int("SEED_SUBSCRIBERS", cls.seed_subscribers),
            aws_region=os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1")),
            sqs_queue_url=os.getenv("SQS_QUEUE_URL", ""),
            sqs_endpoint_url=os.getenv("SQS_ENDPOINT_URL") or None,
            alert_thresholds=_env_thresholds("ALERT_THRESHOLDS", cls.alert_thresholds),
            chaos_enabled=_env_bool("CHAOS_ENABLED", False),
            chaos_latency_ms=_env_int("CHAOS_LATENCY_MS", 0),
            chaos_error_rate=_env_float("CHAOS_ERROR_RATE", 0.0),
            otel_enabled=_env_bool("OTEL_ENABLED", False),
        )
