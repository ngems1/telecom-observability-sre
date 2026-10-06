"""Message handling: parse -> deduplicate -> deliver -> record.

Free of AWS and database imports. ``store`` and ``notifier`` are injected, so
the whole flow is unit-testable with fakes.
"""
from __future__ import annotations

import json
import logging
import time
from enum import Enum
from typing import Protocol

from .chaos import DeliveryError
from .observability import (
    PROCESSED,
    PROCESSING_SECONDS,
    QUEUE_DELAY_SECONDS,
    consumer_span,
    correlation_id_var,
)

log = logging.getLogger(__name__)

REQUIRED_FIELDS = ("event_id", "type", "subscriber_id", "msisdn", "kind", "threshold_pct", "used", "quota")
KIND_LABELS = {"data": "data (MB)", "voice": "voice minutes", "sms": "SMS messages"}
TRACE_KEYS = ("traceparent", "tracestate")


class InvalidEvent(ValueError):
    """The message is not a valid usage event (it will never succeed on retry)."""


class Outcome(str, Enum):
    SENT = "sent"
    DUPLICATE = "duplicate"
    INVALID = "invalid"
    DELIVERY_FAILED = "delivery_failed"
    ERROR = "error"

    @property
    def delete_message(self) -> bool:
        """Only finished work is deleted. Everything else is left for SQS to
        redeliver and, after maxReceiveCount attempts, move to the DLQ."""
        return self in (Outcome.SENT, Outcome.DUPLICATE)


class Store(Protocol):
    def already_sent(self, event_id: str) -> bool: ...

    def record_sent(self, event: dict, message: str, correlation_id: str, queue_delay: float | None) -> bool: ...


class Notifier(Protocol):
    def deliver(self, msisdn: str, text: str) -> None: ...


def parse_event(body: str) -> dict:
    try:
        event = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise InvalidEvent(f"body is not valid JSON: {exc}") from exc
    if not isinstance(event, dict):
        raise InvalidEvent("body is not a JSON object")
    missing = [f for f in REQUIRED_FIELDS if f not in event]
    if missing:
        raise InvalidEvent(f"missing fields: {', '.join(missing)}")
    if event["kind"] not in KIND_LABELS:
        raise InvalidEvent(f"unknown kind: {event['kind']!r}")
    return event


def render_message(event: dict) -> str:
    label = KIND_LABELS[event["kind"]]
    used, quota, pct = event["used"], event["quota"], event["threshold_pct"]
    if pct >= 100:
        return f"You have used all of your {label} allowance ({used}/{quota}). Extra usage may be charged."
    return f"You have used {pct}% of your {label} allowance ({used}/{quota})."


def _attr(attributes: dict, name: str) -> str | None:
    value = attributes.get(name)
    return value.get("StringValue") if isinstance(value, dict) else None


class MessageHandler:
    def __init__(self, store: Store, notifier: Notifier) -> None:
        self._store = store
        self._notifier = notifier

    def handle(
        self,
        body: str,
        attributes: dict,
        sent_timestamp_ms: int | None = None,
        receive_count: int = 1,
    ) -> Outcome:
        correlation_id = _attr(attributes, "correlation_id") or "-"
        token = correlation_id_var.set(correlation_id)
        carrier = {k: v for k in TRACE_KEYS if (v := _attr(attributes, k))}
        started = time.perf_counter()
        outcome = Outcome.ERROR
        try:
            with consumer_span("process usage alert", carrier):
                outcome = self._handle(body, correlation_id, sent_timestamp_ms, receive_count)
            return outcome
        except Exception:  # noqa: BLE001 - never let one bad message kill the worker loop
            log.exception("unexpected error while processing message", extra={"receive_count": receive_count})
            return Outcome.ERROR
        finally:
            PROCESSED.labels(result=outcome.value).inc()
            PROCESSING_SECONDS.observe(time.perf_counter() - started)
            correlation_id_var.reset(token)

    def _handle(
        self, body: str, correlation_id: str, sent_timestamp_ms: int | None, receive_count: int
    ) -> Outcome:
        try:
            event = parse_event(body)
        except InvalidEvent as exc:
            log.error("invalid message", extra={"reason": str(exc), "receive_count": receive_count})
            return Outcome.INVALID

        if correlation_id == "-" and event.get("correlation_id"):
            correlation_id = str(event["correlation_id"])
            correlation_id_var.set(correlation_id)

        event_id = event["event_id"]
        if self._store.already_sent(event_id):
            log.info("duplicate event ignored", extra={"event_id": event_id})
            return Outcome.DUPLICATE

        message = render_message(event)
        try:
            self._notifier.deliver(event["msisdn"], message)
        except DeliveryError as exc:
            log.warning(
                "delivery failed, will be retried",
                extra={"event_id": event_id, "reason": str(exc), "receive_count": receive_count},
            )
            return Outcome.DELIVERY_FAILED

        queue_delay = None
        if sent_timestamp_ms:
            queue_delay = max(0.0, time.time() - sent_timestamp_ms / 1000)
            QUEUE_DELAY_SECONDS.observe(queue_delay)

        if not self._store.record_sent(event, message, correlation_id, queue_delay):
            log.info("duplicate event (race) ignored", extra={"event_id": event_id})
            return Outcome.DUPLICATE

        log.info(
            "notification sent",
            extra={
                "event_id": event_id,
                "subscriber_id": event["subscriber_id"],
                "kind": event["kind"],
                "threshold_pct": event["threshold_pct"],
                "msisdn_suffix": str(event["msisdn"])[-4:],
                "queue_delay_s": round(queue_delay, 3) if queue_delay is not None else None,
            },
        )
        return Outcome.SENT
