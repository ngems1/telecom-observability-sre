"""Structured logging, correlation IDs, Prometheus metrics and optional tracing.

Metric names are part of the project's contract: dashboards and SLO alerts are
written against them (see README, "SLIs and the metrics behind them").
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone

from prometheus_client import Counter, Gauge, Histogram

log = logging.getLogger(__name__)

# Correlation ID of the request being handled. Set by the HTTP middleware,
# copied into every log line and into SQS message attributes.
correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)

# Buckets include 0.3s on purpose: the latency SLO is "95% of requests < 300 ms",
# so the SLI can be computed exactly from the le="0.3" bucket.
LATENCY_BUCKETS = (0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0, 5.0)

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=LATENCY_BUCKETS,
)
HTTP_IN_FLIGHT = Gauge("http_requests_in_flight", "HTTP requests currently being handled")

DB_QUERY_LATENCY = Histogram(
    "db_query_duration_seconds",
    "Database statement latency",
    ["operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
DB_POOL_IN_USE = Gauge("db_pool_connections_in_use", "Database connections checked out")

USAGE_RECORDS = Counter("usage_records_total", "Usage records accepted", ["kind"])
ALERT_EVENTS = Counter(
    "usage_alert_events_total", "Threshold events produced", ["kind", "threshold"]
)
SQS_PUBLISH = Counter("sqs_publish_total", "SQS publish attempts", ["result"])
SQS_PUBLISH_LATENCY = Histogram(
    "sqs_publish_duration_seconds",
    "SQS send_message latency",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
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
    """One JSON object per line: easy to ship to CloudWatch Logs and query with Logs Insights."""

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
    # Route uvicorn's own loggers through our JSON handler; request logging is
    # done by the middleware (with route, status, latency and correlation ID).
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for noisy in ("botocore", "boto3", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def inject_trace_headers() -> dict[str, str]:
    """W3C trace-context headers for the current span (empty when tracing is off)."""
    try:
        from opentelemetry.propagate import inject
    except ImportError:
        return {}
    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier


def extract_trace_context(carrier: dict[str, str]):
    try:
        from opentelemetry.propagate import extract
    except ImportError:
        return None
    return extract(carrier)


def setup_tracing(service: str, environment: str, enabled: bool, app=None, engine=None) -> bool:
    """Enable OpenTelemetry when OTEL_ENABLED=true and the packages are installed.

    The OTLP endpoint is taken from the standard OTEL_EXPORTER_OTLP_ENDPOINT
    environment variable.
    """
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
