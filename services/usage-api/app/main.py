"""FastAPI application factory.

Run with:  uvicorn --factory app.main:create_app
"""
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from starlette.routing import Match

from . import __version__, service
from .chaos import ChaosState
from .config import Settings
from .db import init_db, make_engine, make_session_factory
from .events import build_publisher
from .models import Plan, Subscriber
from .observability import (
    HTTP_IN_FLIGHT,
    HTTP_LATENCY,
    HTTP_REQUESTS,
    correlation_id_var,
    setup_logging,
    setup_tracing,
)
from .schemas import (
    PlanOut,
    SubscriberDetail,
    SubscriberIn,
    SubscriberOut,
    UsageIn,
    UsageOut,
)

log = logging.getLogger("usage_api")

# Probe, scrape and control paths: not subject to failure injection, and not
# access-logged at INFO (they would drown real traffic).
QUIET_PATHS = ("/healthz", "/readyz", "/metrics")
CHAOS_EXEMPT_PREFIXES = QUIET_PATHS + ("/chaos", "/demo")


class LatencyIn(BaseModel):
    delay_ms: int = Field(ge=0, le=30000)
    probability: float = Field(default=1.0, ge=0, le=1)


class ErrorsIn(BaseModel):
    rate: float = Field(ge=0, le=1)


def _route_label(app: FastAPI, request: Request) -> str:
    """Route template (e.g. /v1/subscribers/{subscriber_id}) to keep label cardinality bounded."""
    route = request.scope.get("route")
    if route is not None:
        return route.path
    for candidate in app.router.routes:
        match, _ = candidate.matches(request.scope)
        if match == Match.FULL:
            return candidate.path
    return "unmatched"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    setup_logging(settings.service_name, settings.environment, settings.log_level)

    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    publisher = build_publisher(settings)
    chaos = ChaosState(latency_ms=settings.chaos_latency_ms, error_rate=settings.chaos_error_rate)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if settings.auto_migrate:
            init_db(engine, session_factory, settings.seed_subscribers)
        log.info(
            "usage-api started",
            extra={"version": __version__, "chaos_enabled": settings.chaos_enabled},
        )
        yield
        engine.dispose()

    app = FastAPI(title="Telecom Usage API", version=__version__, lifespan=lifespan)

    # ------------------------------------------------------------------ middleware
    @app.middleware("http")
    async def observe(request: Request, call_next):
        correlation_id = (
            request.headers.get("x-correlation-id")
            or request.headers.get("x-request-id")
            or str(uuid.uuid4())
        )[:64]
        token = correlation_id_var.set(correlation_id)
        path = request.url.path
        start = time.perf_counter()
        status = 500
        HTTP_IN_FLIGHT.inc()
        try:
            response = None
            if settings.chaos_enabled and not path.startswith(CHAOS_EXEMPT_PREFIXES):
                response = await chaos.apply()
            if response is None:
                response = await call_next(request)
            status = response.status_code
            response.headers["X-Correlation-ID"] = correlation_id
            return response
        except Exception:
            log.exception("unhandled error", extra={"path": path})
            raise
        finally:
            elapsed = time.perf_counter() - start
            route = _route_label(app, request)
            HTTP_REQUESTS.labels(method=request.method, route=route, status=str(status)).inc()
            HTTP_LATENCY.labels(method=request.method, route=route).observe(elapsed)
            HTTP_IN_FLIGHT.dec()
            level = logging.DEBUG if path in QUIET_PATHS else logging.INFO
            log.log(
                level,
                "request",
                extra={
                    "http_method": request.method,
                    "route": route,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 1),
                },
            )
            correlation_id_var.reset(token)

    setup_tracing(settings.service_name, settings.environment, settings.otel_enabled, app, engine)

    # ------------------------------------------------------------------ deps
    def get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    queue_cache = {"checked_at": 0.0, "state": "disabled"}

    def queue_state() -> str:
        """SQS health, cached so probes don't hammer AWS. Never fails readiness."""
        if not publisher.enabled:
            return "disabled"
        now = time.monotonic()
        if now - queue_cache["checked_at"] > 15:
            queue_cache["state"] = "ok" if publisher.check() else "degraded"
            queue_cache["checked_at"] = now
        return queue_cache["state"]

    # ------------------------------------------------------------------ operational endpoints
    @app.get("/healthz", tags=["ops"])
    def healthz():
        """Liveness: the process is up. Deliberately does not touch the database."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    def readyz(response: Response):
        """Readiness: can serve traffic. The database is critical; the queue is reported only."""
        deps = {}
        ready = True
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            deps["database"] = "ok"
        except Exception as exc:  # noqa: BLE001
            ready = False
            deps["database"] = "unavailable"
            log.warning("readiness: database check failed: %s", exc)
        deps["queue"] = queue_state()
        if not ready:
            response.status_code = 503
        return {"status": "ready" if ready else "not_ready", "dependencies": deps}

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # ------------------------------------------------------------------ business endpoints
    @app.get("/v1/plans", response_model=list[PlanOut], tags=["plans"])
    def list_plans(db: Session = Depends(get_db)):
        return db.scalars(select(Plan).order_by(Plan.monthly_price_cents)).all()

    @app.get("/v1/subscribers", response_model=list[SubscriberOut], tags=["subscribers"])
    def list_subscribers(limit: int = Query(50, ge=1, le=500), db: Session = Depends(get_db)):
        return db.scalars(select(Subscriber).order_by(Subscriber.msisdn).limit(limit)).all()

    @app.post("/v1/subscribers", response_model=SubscriberOut, status_code=201, tags=["subscribers"])
    def create_subscriber(body: SubscriberIn, db: Session = Depends(get_db)):
        try:
            return service.create_subscriber(db, body.msisdn, body.name, body.plan_id)
        except service.NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except service.ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/subscribers/{subscriber_id}", response_model=SubscriberDetail, tags=["subscribers"])
    def get_subscriber(subscriber_id: str, db: Session = Depends(get_db)):
        sub = db.get(Subscriber, subscriber_id)
        if sub is None:
            raise HTTPException(status_code=404, detail="subscriber not found")
        return SubscriberDetail(
            id=sub.id,
            msisdn=sub.msisdn,
            name=sub.name,
            plan_id=sub.plan_id,
            usage=service.usage_summary(db, sub),
        )

    @app.post("/v1/usage", response_model=UsageOut, status_code=201, tags=["usage"])
    def record_usage(body: UsageIn, db: Session = Depends(get_db)):
        try:
            return service.record_usage(
                db,
                publisher,
                settings.alert_thresholds,
                body.subscriber_id,
                body.kind,
                body.amount,
                correlation_id_var.get(),
            )
        except service.NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ------------------------------------------------------------------ chaos / demo tools
    if settings.chaos_enabled:

        @app.get("/chaos", tags=["chaos"])
        def chaos_status():
            return chaos.snapshot()

        @app.post("/chaos/latency", tags=["chaos"])
        def chaos_latency(body: LatencyIn):
            chaos.set_latency(body.delay_ms, body.probability)
            log.warning("chaos: latency injection set", extra=chaos.snapshot())
            return chaos.snapshot()

        @app.post("/chaos/errors", tags=["chaos"])
        def chaos_errors(body: ErrorsIn):
            chaos.set_error_rate(body.rate)
            log.warning("chaos: error injection set", extra=chaos.snapshot())
            return chaos.snapshot()

        @app.post("/chaos/reset", tags=["chaos"])
        def chaos_reset():
            chaos.reset()
            log.warning("chaos: all injections cleared")
            return chaos.snapshot()

        @app.post("/chaos/poison-message", tags=["chaos"])
        def chaos_poison(kind: Literal["malformed", "incomplete"] = "malformed"):
            """Publish a message the Notification service cannot process.

            It is retried until SQS moves it to the dead-letter queue.
            """
            body = "{this is not json" if kind == "malformed" else '{"type": "usage.threshold_crossed"}'
            try:
                publisher.publish_raw(body, correlation_id_var.get())
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=f"publish failed: {exc}") from exc
            log.warning("chaos: poison message published", extra={"poison_kind": kind})
            return {"published": kind}

        @app.post("/chaos/crash", tags=["chaos"])
        def chaos_crash():
            """Hard-kill this process (Kubernetes restarts the container)."""
            import os
            import threading

            log.critical("chaos: crashing process on request")
            threading.Timer(0.2, lambda: os._exit(1)).start()
            return {"crashing": True}

        @app.post("/demo/reset-usage", tags=["demo"])
        def demo_reset_usage(db: Session = Depends(get_db)):
            """Start a new allowance cycle so threshold alerts can fire again."""
            return {"reset_rows": service.reset_usage(db)}

    return app
