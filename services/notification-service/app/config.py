"""Runtime configuration, read from environment variables."""
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


@dataclass(frozen=True)
class Settings:
    service_name: str = "notification-service"
    environment: str = "dev"
    log_level: str = "INFO"

    database_url: str = "sqlite+pysqlite:///./notifications.db"
    db_pool_size: int = 3
    db_max_overflow: int = 2

    aws_region: str = "us-east-1"
    sqs_queue_url: str = ""
    sqs_endpoint_url: str | None = None
    wait_seconds: int = 10  # SQS long-poll duration
    stale_seconds: int = 60  # liveness fails if the worker loop stops beating for this long
    worker_enabled: bool = True

    chaos_enabled: bool = False
    chaos_stalled: bool = False
    chaos_failure_rate: float = 0.0
    chaos_delivery_latency_ms: int = 0

    otel_enabled: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            service_name=os.getenv("SERVICE_NAME", "notification-service"),
            environment=os.getenv("ENVIRONMENT", "dev"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            database_url=os.getenv("DATABASE_URL", cls.database_url),
            db_pool_size=_env_int("DB_POOL_SIZE", cls.db_pool_size),
            db_max_overflow=_env_int("DB_MAX_OVERFLOW", cls.db_max_overflow),
            aws_region=os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1")),
            sqs_queue_url=os.getenv("SQS_QUEUE_URL", ""),
            sqs_endpoint_url=os.getenv("SQS_ENDPOINT_URL") or None,
            wait_seconds=_env_int("SQS_WAIT_SECONDS", cls.wait_seconds),
            stale_seconds=_env_int("WORKER_STALE_SECONDS", cls.stale_seconds),
            worker_enabled=_env_bool("WORKER_ENABLED", True),
            chaos_enabled=_env_bool("CHAOS_ENABLED", False),
            chaos_stalled=_env_bool("CHAOS_STALLED", False),
            chaos_failure_rate=_env_float("CHAOS_FAILURE_RATE", 0.0),
            chaos_delivery_latency_ms=_env_int("CHAOS_DELIVERY_LATENCY_MS", 0),
            otel_enabled=_env_bool("OTEL_ENABLED", False),
        )
