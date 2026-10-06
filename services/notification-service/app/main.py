"""Notification service: SQS consumer plus a small HTTP surface for probes, metrics and chaos.

Run with:  uvicorn --factory app.main:create_app --port 8081
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from . import __version__
from .chaos import SimulatedNotifier, WorkerChaos
from .config import Settings
from .observability import setup_logging, setup_tracing
from .processor import MessageHandler
from .store import NotificationStore, init_schema, make_engine
from .worker import Worker, build_sqs_client

log = logging.getLogger("notification_service")


class StallIn(BaseModel):
    enabled: bool


class FailuresIn(BaseModel):
    rate: float = Field(ge=0, le=1)


class LatencyIn(BaseModel):
    delay_ms: int = Field(ge=0, le=30000)


def create_app(settings: Settings | None = None, sqs_client=None) -> FastAPI:
    settings = settings or Settings.from_env()
    setup_logging(settings.service_name, settings.environment, settings.log_level)

    engine = make_engine(settings)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    chaos = WorkerChaos(
        stalled=settings.chaos_stalled,
        failure_rate=settings.chaos_failure_rate,
        delivery_latency_ms=settings.chaos_delivery_latency_ms,
    )
    handler = MessageHandler(NotificationStore(session_factory), SimulatedNotifier(chaos))

    worker: Worker | None = None
    if settings.worker_enabled and settings.sqs_queue_url:
        worker = Worker(settings, sqs_client or build_sqs_client(settings), handler, chaos)
    elif settings.worker_enabled:
        log.warning("SQS_QUEUE_URL is not set: the worker will not start")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_schema(engine)
        if worker is not None:
            worker.start()
        log.info("notification-service started", extra={"version": __version__, "worker": worker is not None})
        yield
        if worker is not None:
            # SIGTERM -> finish the current message, stop polling. Whatever is in flight
            # but unfinished is redelivered by SQS after the visibility timeout.
            worker.stop()
            worker.join(timeout=25)
        engine.dispose()

    app = FastAPI(title="Telecom Notification Service", version=__version__, lifespan=lifespan)
    setup_tracing(settings.service_name, settings.environment, settings.otel_enabled, app, engine)

    @app.get("/healthz", tags=["ops"])
    def healthz(response: Response):
        """Liveness: the consumer loop is alive and still iterating."""
        if worker is not None and not worker.healthy():
            response.status_code = 503
            return {"status": "worker_unhealthy", "heartbeat_age_s": round(worker.heartbeat_age(), 1)}
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    def readyz(response: Response):
        """Readiness: database reachable and worker running.

        A stalled worker (failure injection) is still "ready" on purpose: it
        models a consumer that is wedged but looks healthy to Kubernetes.
        """
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
        if worker is not None:
            deps["worker"] = "ok" if worker.healthy() else "down"
            ready = ready and worker.healthy()
        else:
            deps["worker"] = "disabled"
        if not ready:
            response.status_code = 503
        return {"status": "ready" if ready else "not_ready", "dependencies": deps}

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    if settings.chaos_enabled:

        @app.get("/chaos", tags=["chaos"])
        def chaos_status():
            return chaos.snapshot()

        @app.post("/chaos/stall", tags=["chaos"])
        def chaos_stall(body: StallIn):
            """Stop consuming from SQS while staying healthy and Ready (stuck-queue scenario)."""
            chaos.set_stalled(body.enabled)
            log.warning("chaos: stall set", extra=chaos.snapshot())
            return chaos.snapshot()

        @app.post("/chaos/failures", tags=["chaos"])
        def chaos_failures(body: FailuresIn):
            """Make the SMS provider reject a fraction of deliveries (messages retry, then hit the DLQ)."""
            chaos.set_failure_rate(body.rate)
            log.warning("chaos: delivery failure rate set", extra=chaos.snapshot())
            return chaos.snapshot()

        @app.post("/chaos/latency", tags=["chaos"])
        def chaos_latency(body: LatencyIn):
            chaos.set_delivery_latency(body.delay_ms)
            log.warning("chaos: delivery latency set", extra=chaos.snapshot())
            return chaos.snapshot()

        @app.post("/chaos/reset", tags=["chaos"])
        def chaos_reset():
            chaos.reset()
            log.warning("chaos: all injections cleared")
            return chaos.snapshot()

    return app
