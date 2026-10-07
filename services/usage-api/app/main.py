"""FastAPI application factory.

Run with:  uvicorn --factory app.main:create_app
"""
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
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
from .logic import BUNDLES
from .models import Plan, Subscriber, TopUp
from .payments import SimulatedPaymentProvider
from .observability import (
    HTTP_IN_FLIGHT,
    HTTP_LATENCY,
    HTTP_REQUESTS,
    correlation_id_var,
    setup_logging,
    setup_tracing,
)
from .schemas import (
    BalanceOut,
    BundleOut,
    BundlePurchaseIn,
    BundlePurchaseOut,
    PlanOut,
    SubscriberDetail,
    SubscriberIn,
    SubscriberOut,
    TopUpIn,
    TopUpOut,
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


class PaymentsChaosIn(BaseModel):
    latency_ms: int = Field(default=0, ge=0, le=30000)
    error_rate: float = Field(default=0.0, ge=0, le=1)
    decline_rate: float = Field(default=0.0, ge=0, le=1)


# HTTP status for each top-up outcome. Declines are the customer's bank saying no (4xx, not an SLO error);
# provider failures are ours to fix (5xx: they burn the availability budget and page someone).
TOPUP_STATUS_CODES = {
    "succeeded": 201,
    "declined": 402,
    "provider_error": 502,
    "provider_timeout": 504,
}
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9_.:-]{1,64}$"


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
    payments = SimulatedPaymentProvider(
        timeout_s=settings.payment_timeout_ms / 1000, decline_rate=settings.payment_decline_rate
    )

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
    def list_subscribers(
        limit: int = Query(50, ge=1, le=500),
        msisdn: str | None = Query(None, pattern=r"^\+?[0-9]{8,15}$", description="Find the account of a phone number"),
        db: Session = Depends(get_db),
    ):
        stmt = select(Subscriber).order_by(Subscriber.msisdn).limit(limit)
        if msisdn:
            stmt = stmt.where(Subscriber.msisdn == msisdn)
        return db.scalars(stmt).all()

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
        wallet = service.get_wallet(db, sub.id)
        detail = SubscriberDetail(
            id=sub.id,
            msisdn=sub.msisdn,
            name=sub.name,
            plan_id=sub.plan_id,
            usage=service.usage_summary(db, sub),
            balance_cents=wallet.balance_cents,
            currency=wallet.currency,
            active_bundles=service.active_bundles(db, sub.id),
        )
        db.commit()  # persists a wallet created for a subscriber that predates wallets
        return detail

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

    # ------------------------------------------------------------------ self-care: balance, top-ups, bundles
    @app.get("/v1/subscribers/{subscriber_id}/balance", response_model=BalanceOut, tags=["self-care"])
    def get_balance(subscriber_id: str, db: Session = Depends(get_db)):
        try:
            service.get_subscriber(db, subscriber_id)
        except service.NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        wallet = service.get_wallet(db, subscriber_id)
        db.commit()
        return BalanceOut(subscriber_id=subscriber_id, balance_cents=wallet.balance_cents, currency=wallet.currency)

    @app.post(
        "/v1/topups",
        response_model=TopUpOut,
        status_code=201,
        tags=["self-care"],
        responses={
            200: {"description": "Repeated request (same Idempotency-Key): the first result, nothing charged again"},
            402: {"description": "Payment declined by the customer's bank or wallet"},
            502: {"description": "Payment provider error"},
            504: {"description": "Payment provider timeout"},
        },
    )
    def create_topup(
        body: TopUpIn,
        response: Response,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            pattern=IDEMPOTENCY_KEY_PATTERN,
            description="Client-generated key. Send the same key when retrying so the customer is charged once.",
        ),
        db: Session = Depends(get_db),
    ):
        try:
            topup, replayed = service.request_topup(
                db,
                publisher,
                payments,
                subscriber_id=body.subscriber_id,
                amount_cents=body.amount_cents,
                method=body.payment_method,
                idempotency_key=idempotency_key or f"server-{uuid.uuid4()}",
                currency=settings.currency,
                correlation_id=correlation_id_var.get(),
            )
        except service.NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if replayed:
            response.status_code = 200
            response.headers["Idempotent-Replayed"] = "true"
        else:
            response.status_code = TOPUP_STATUS_CODES.get(
                "succeeded" if topup.status == "succeeded" else topup.failure_reason or "provider_error", 502
            )
        return topup

    @app.get("/v1/topups/{topup_id}", response_model=TopUpOut, tags=["self-care"])
    def get_topup(topup_id: str, db: Session = Depends(get_db)):
        topup = db.get(TopUp, topup_id)
        if topup is None:
            raise HTTPException(status_code=404, detail="top-up not found")
        return topup

    @app.get("/v1/subscribers/{subscriber_id}/topups", response_model=list[TopUpOut], tags=["self-care"])
    def topup_history(subscriber_id: str, limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
        try:
            return service.list_topups(db, subscriber_id, limit)
        except service.NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/bundles", response_model=list[BundleOut], tags=["self-care"])
    def list_bundles():
        return [BundleOut(id=bundle_id, **{k: b[k] for k in ("name", "kind", "amount", "price_cents")})
                for bundle_id, b in BUNDLES.items()]

    @app.post(
        "/v1/subscribers/{subscriber_id}/bundles",
        response_model=BundlePurchaseOut,
        status_code=201,
        tags=["self-care"],
        responses={402: {"description": "Balance too low: top up first"}},
    )
    def buy_bundle(subscriber_id: str, body: BundlePurchaseIn, db: Session = Depends(get_db)):
        try:
            return service.purchase_bundle(
                db,
                publisher,
                subscriber_id=subscriber_id,
                bundle_id=body.bundle_id,
                currency=settings.currency,
                correlation_id=correlation_id_var.get(),
            )
        except service.NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except service.InsufficientFunds as exc:
            raise HTTPException(status_code=402, detail=str(exc)) from exc

    # ------------------------------------------------------------------ chaos / demo tools
    if settings.chaos_enabled:

        @app.get("/chaos", tags=["chaos"])
        def chaos_status():
            return {**chaos.snapshot(), "payments": payments.chaos_snapshot()}

        @app.post("/chaos/payments", tags=["chaos"])
        def chaos_payments(body: PaymentsChaosIn):
            """Degrade the payment provider: slow (latency_ms; above the timeout = 504), failing (error_rate = 502)
            or declining more cards (decline_rate = 402)."""
            payments.set_chaos(body.latency_ms, body.error_rate, body.decline_rate)
            log.warning("chaos: payment provider degraded", extra=payments.chaos_snapshot())
            return payments.chaos_snapshot()

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
            payments.reset_chaos()
            log.warning("chaos: all injections cleared")
            return {**chaos.snapshot(), "payments": payments.chaos_snapshot()}

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
            """Start a new allowance cycle (usage to 0, bundles expire) so threshold alerts can fire again."""
            return {"reset_rows": service.reset_usage(db)}

    return app
