"""Engine, session factory and schema bootstrap."""
from __future__ import annotations

import logging
import time

from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import Settings
from .logic import KINDS
from .models import Base, Plan, Subscriber, UsageTotal, Wallet
from .observability import DB_POOL_IN_USE, DB_QUERY_LATENCY

log = logging.getLogger(__name__)

_OPERATIONS = {"SELECT", "INSERT", "UPDATE", "DELETE", "BEGIN", "COMMIT", "ROLLBACK"}

DEFAULT_PLANS = [
    {"id": "basic", "name": "Basic", "data_mb": 2048, "voice_min": 200, "sms": 100, "monthly_price_cents": 1500},
    {"id": "plus", "name": "Plus", "data_mb": 10240, "voice_min": 1000, "sms": 500, "monthly_price_cents": 3500},
    {"id": "max", "name": "Max", "data_mb": 51200, "voice_min": 5000, "sms": 2000, "monthly_price_cents": 6500},
]


def make_engine(settings: Settings) -> Engine:
    url = settings.database_url
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url or url.rstrip("/") in ("sqlite:", "sqlite+pysqlite:"):
            kwargs["poolclass"] = StaticPool  # one shared in-memory database
    else:
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=5,
            pool_recycle=1800,
            connect_args={"connect_timeout": 5},
        )
    engine = create_engine_with_metrics(url, kwargs)
    if hasattr(engine.pool, "checkedout"):
        DB_POOL_IN_USE.set_function(engine.pool.checkedout)
    return engine


def create_engine_with_metrics(url: str, kwargs: dict) -> Engine:
    from sqlalchemy import create_engine

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

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(
    engine: Engine,
    session_factory: sessionmaker[Session],
    seed_subscribers: int,
    retries: int = 30,
    delay: float = 2.0,
) -> None:
    """Create tables and seed demo data.

    Retries because the database may still be starting (docker-compose) and
    because several replicas can start at once. Demo-grade: a real system
    would use migrations (Alembic) run by a pre-deploy Job.
    """
    for attempt in range(1, retries + 1):
        try:
            Base.metadata.create_all(engine)
            break
        except Exception as exc:  # noqa: BLE001 - retry on any startup race / connectivity error
            log.warning("database not ready (attempt %d/%d): %s", attempt, retries, exc)
            if attempt == retries:
                raise
            time.sleep(delay)
    seed(session_factory, seed_subscribers)


def seed(session_factory: sessionmaker[Session], subscriber_count: int) -> None:
    import uuid

    with session_factory() as db:
        for plan in DEFAULT_PLANS:
            if db.get(Plan, plan["id"]) is None:
                db.add(Plan(**plan))
        try:
            db.commit()
        except Exception:  # noqa: BLE001 - another replica seeded first
            db.rollback()

    plan_ids = [p["id"] for p in DEFAULT_PLANS]
    for i in range(subscriber_count):
        msisdn = f"+155501{i:05d}"
        sub_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, msisdn))  # deterministic across replicas
        opening_balance = 500 + (i * 731) % 3000  # 5.00 to 34.99, varied so bundle purchases sometimes fail
        with session_factory() as db:
            existing = db.scalar(select(Subscriber.id).where(Subscriber.msisdn == msisdn))
            if existing:
                # Databases created before wallets existed: give seeded subscribers their opening balance once.
                if db.get(Wallet, existing) is None:
                    db.add(Wallet(subscriber_id=existing, balance_cents=opening_balance))
                    try:
                        db.commit()
                    except Exception:  # noqa: BLE001 - another replica did it first
                        db.rollback()
                continue
            db.add(
                Subscriber(
                    id=sub_id,
                    msisdn=msisdn,
                    name=f"Demo Subscriber {i:03d}",
                    plan_id=plan_ids[i % len(plan_ids)],
                )
            )
            for kind in KINDS:
                db.add(UsageTotal(subscriber_id=sub_id, kind=kind, used=0))
            db.add(Wallet(subscriber_id=sub_id, balance_cents=opening_balance))
            try:
                db.commit()
            except Exception:  # noqa: BLE001 - another replica seeded first
                db.rollback()
