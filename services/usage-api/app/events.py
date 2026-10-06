"""Publishing events to SQS."""
from __future__ import annotations

import json
import logging
import time
from typing import Protocol

from .config import Settings
from .observability import SQS_PUBLISH, SQS_PUBLISH_LATENCY, inject_trace_headers

log = logging.getLogger(__name__)


class Publisher(Protocol):
    enabled: bool

    def publish(self, event: dict, correlation_id: str) -> None: ...

    def publish_raw(self, body: str, correlation_id: str) -> None: ...

    def check(self) -> bool: ...


class NullPublisher:
    """Used when no queue is configured: events are logged and dropped."""

    enabled = False

    def publish(self, event: dict, correlation_id: str) -> None:
        log.info("event dropped (no queue configured)", extra={"event_type": event.get("type")})

    def publish_raw(self, body: str, correlation_id: str) -> None:
        log.info("raw message dropped (no queue configured)")

    def check(self) -> bool:
        return True


class SqsPublisher:
    enabled = True

    def __init__(self, queue_url: str, region: str, endpoint_url: str | None = None, client=None):
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "sqs",
                region_name=region,
                endpoint_url=endpoint_url,
                # Fail fast: a slow queue must not hold API worker threads for long.
                config=Config(
                    connect_timeout=2, read_timeout=5, retries={"max_attempts": 2, "mode": "standard"}
                ),
            )
        self._client = client
        self._queue_url = queue_url

    def _attributes(self, correlation_id: str) -> dict:
        attrs = {"correlation_id": {"DataType": "String", "StringValue": correlation_id}}
        for key, value in inject_trace_headers().items():
            attrs[key] = {"DataType": "String", "StringValue": value}
        return attrs

    def publish_raw(self, body: str, correlation_id: str) -> None:
        start = time.perf_counter()
        try:
            self._client.send_message(
                QueueUrl=self._queue_url,
                MessageBody=body,
                MessageAttributes=self._attributes(correlation_id),
            )
        except Exception:
            SQS_PUBLISH.labels(result="error").inc()
            raise
        finally:
            SQS_PUBLISH_LATENCY.observe(time.perf_counter() - start)
        SQS_PUBLISH.labels(result="ok").inc()

    def publish(self, event: dict, correlation_id: str) -> None:
        self.publish_raw(json.dumps(event), correlation_id)

    def check(self) -> bool:
        try:
            self._client.get_queue_attributes(QueueUrl=self._queue_url, AttributeNames=["QueueArn"])
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("queue check failed: %s", exc)
            return False


def build_publisher(settings: Settings) -> Publisher:
    if not settings.sqs_queue_url:
        log.warning("SQS_QUEUE_URL is not set: usage alerts will not be published")
        return NullPublisher()
    return SqsPublisher(settings.sqs_queue_url, settings.aws_region, settings.sqs_endpoint_url)
