"""Notification audit log in the shared PostgreSQL instance (its own table)."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text, create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import Settings
from .observability import DB_POOL_IN_USE, DB_QUERY_LATENCY

log = logging.getLogger(__name__)

_OPERATIONS = {"SELECT", "INSERT", "UPDATE", "DELETE", "BEGIN", "COMMIT", "ROLLBACK"}


class Base(DeclarativeBase):
    pass


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Unique: this is what makes redelivered SQS messages idempotent.
    event_id: Mapped[str] = mapped_column(String(36), unique=True)
    subscriber_id: Mapped[str] = mapped_column(String(36))
    msisdn: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(8))
    threshold_pct: Mapped[int] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="sent")
    correlation_id: Mapped[str] = mapped_column(String(64), default="-")
    queue_delay_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


def make_engine(settings: Settings) -> Engine:
    url = settings.database_url
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url or url.rstrip("/") in ("sqlite:", "sqlite+pysqlite:"):
            kwargs["poolclass"] = StaticPool
    else:
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=5,
            pool_recycle=1800,
            connect_args={"connect_timeout": 5},
        )
    engine = create_engine(url, **kwargs)

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        conn.info["_query_start"] = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        start = conn.info.pop("_query_start", None)
        if start is None:
            return
        words = statement.lstrip().split(None, 1)
        op = words[0].upper() if words else "OTHER"
        DB_QUERY_LATENCY.labels(operation=op if op in _OPERATIONS else "OTHER").observe(
            time.perf_counter() - start
        )

    if hasattr(engine.pool, "checkedout"):
        DB_POOL_IN_USE.set_function(engine.pool.checkedout)
    return engine


def init_schema(engine: Engine, retries: int = 30, delay: float = 2.0) -> None:
    """Create the table, tolerating a database that is still starting or a startup race."""
    for attempt in range(1, retries + 1):
        try:
            Base.metadata.create_all(engine)
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("database not ready (attempt %d/%d): %s", attempt, retries, exc)
            if attempt == retries:
                raise
            time.sleep(delay)


class NotificationStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def already_sent(self, event_id: str) -> bool:
        with self._session_factory() as db:
            return db.scalar(select(Notification.id).where(Notification.event_id == event_id)) is not None

    def record_sent(
        self, event: dict, message: str, correlation_id: str, queue_delay: float | None
    ) -> bool:
        """Returns False if this event was already recorded (concurrent redelivery)."""
        with self._session_factory() as db:
            db.add(
                Notification(
                    event_id=event["event_id"],
                    subscriber_id=event["subscriber_id"],
                    msisdn=event["msisdn"],
                    kind=event["kind"],
                    threshold_pct=int(event["threshold_pct"]),
                    message=message,
                    status="sent",
                    correlation_id=correlation_id[:64],
                    queue_delay_seconds=queue_delay,
                )
            )
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return False
        return True

    def count(self) -> int:
        from sqlalchemy import func

        with self._session_factory() as db:
            return db.scalar(select(func.count()).select_from(Notification)) or 0
