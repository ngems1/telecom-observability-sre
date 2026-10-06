"""Structured logging, correlation IDs, Prometheus metrics and optional tracing."""
from __future__ import annotations

import contextvars
import json
import logging
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

from prometheus_client import Counter, Gauge, Histogram

log = logging.getLogger(__name__)

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)

# --- Worker / queue metrics -------------------------------------------------
SQS_RECEIVED = Counter("sqs_messages_received_total", "Messages received from SQS")
SQS_RECEIVE_ERRORS = Counter("sqs_receive_errors_total", "Failed SQS receive calls")
SQS_DELETE_ERRORS = Counter("sqs_delete_errors_total", "Failed SQS delete calls")

# result: sent | duplicate | invalid | delivery_failed | error
PROCESSED = Counter("notifications_processed_total", "Messages processed", ["result"])
PROCESSING_SECONDS = Histogram(
    "notification_processing_seconds",
    "Time to process one message",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
# Event produced -> notification delivered. This is the "notification freshness" SLI.
QUEUE_DELAY_SECONDS = Histogram(
    "notification_queue_delay_seconds",
    "Seconds between the API publishing an event and its notification being sent",
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800),
)
WORKER_LAST_POLL = Gauge(
    "notification_worker_last_poll_timestamp_seconds", "Unix time of the worker's last loop iteration"
)
WORKER_STALLED = Gauge("notification_worker_stalled", "1 while the consumer is paused by failure injection")

DB_QUERY_LATENCY = Histogram(
    "db_query_duration_seconds",
    "Database statement latency",
    ["operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
DB_POOL_IN_USE = Gauge("db_pool_connections_in_use", "Database connections checked out")
CHAOS_ACTIVE = Gauge("chaos_injection_active", "1 while a failure injection is active", ["type"])

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


def _trace_fields() -> dict[str, str]:
    try:
        from opentelemetry import trace
    except ImportError:
        return {}
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return {}
    return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, environment: str) -> None:
        super().__init__()
        self.service = service
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "service": self.service,
            "env": self.environment,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": correlation_id_var.get(),
        }
        payload.update(_trace_fields())
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(service: str, environment: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service, environment))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for noisy in ("botocore", "boto3", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@contextmanager
def consumer_span(name: str, carrier: dict[str, str]):
    """Continue the producer's trace (W3C trace-context carried in SQS attributes)."""
    try:
        from opentelemetry import trace
        from opentelemetry.propagate import extract
    except ImportError:
        yield None
        return
    tracer = trace.get_tracer("notification-service")
    with tracer.start_as_current_span(
        name, context=extract(carrier), kind=trace.SpanKind.CONSUMER
    ) as span:
        yield span


def setup_tracing(service: str, environment: str, enabled: bool, app=None, engine=None) -> bool:
    if not enabled:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        log.warning("OTEL_ENABLED is true but OpenTelemetry packages are missing: %s", exc)
        return False

    provider = TracerProvider(
        resource=Resource.create({"service.name": service, "deployment.environment": environment})
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    if app is not None:
        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
    if engine is not None:
        SQLAlchemyInstrumentor().instrument(engine=engine)
    BotocoreInstrumentor().instrument()
    log.info("OpenTelemetry tracing enabled")
    return True
