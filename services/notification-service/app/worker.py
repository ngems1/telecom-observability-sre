"""SQS consumer loop running in a background thread."""
from __future__ import annotations

import logging
import threading
import time

from .chaos import WorkerChaos
from .config import Settings
from .observability import SQS_DELETE_ERRORS, SQS_RECEIVE_ERRORS, SQS_RECEIVED, WORKER_LAST_POLL
from .processor import MessageHandler, Outcome

log = logging.getLogger(__name__)


def build_sqs_client(settings: Settings):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "sqs",
        region_name=settings.aws_region,
        endpoint_url=settings.sqs_endpoint_url,
        # read_timeout must exceed the long-poll wait or every idle poll would time out.
        config=Config(
            connect_timeout=3,
            read_timeout=settings.wait_seconds + 10,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


class Worker(threading.Thread):
    def __init__(self, settings: Settings, sqs_client, handler: MessageHandler, chaos: WorkerChaos) -> None:
        super().__init__(name="sqs-worker", daemon=True)
        self._settings = settings
        self._sqs = sqs_client
        self._handler = handler
        self._chaos = chaos
        self._stop_event = threading.Event()
        self.last_heartbeat = time.monotonic()

    # -- health ---------------------------------------------------------
    def beat(self) -> None:
        self.last_heartbeat = time.monotonic()
        WORKER_LAST_POLL.set(time.time())

    def heartbeat_age(self) -> float:
        return time.monotonic() - self.last_heartbeat

    def healthy(self) -> bool:
        """Alive and still looping. A *stalled* worker (chaos) keeps beating on purpose."""
        return self.is_alive() and self.heartbeat_age() < self._settings.stale_seconds

    def stop(self) -> None:
        self._stop_event.set()

    # -- loop -------------------------------------------------------------
    def run(self) -> None:
        log.info("worker started", extra={"queue_url": self._settings.sqs_queue_url})
        self.beat()
        while not self._stop_event.is_set():
            self.beat()
            if self._chaos.stalled:
                self._stop_event.wait(1.0)
                continue
            try:
                messages = self._receive()
            except Exception:  # noqa: BLE001
                SQS_RECEIVE_ERRORS.inc()
                log.exception("sqs receive failed")
                self._stop_event.wait(5.0)
                continue
            for message in messages:
                self.beat()
                self._process(message)
                if self._stop_event.is_set():
                    break  # unprocessed messages reappear after the visibility timeout
        log.info("worker stopped")

    def _receive(self) -> list[dict]:
        response = self._sqs.receive_message(
            QueueUrl=self._settings.sqs_queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=self._settings.wait_seconds,
            MessageAttributeNames=["All"],
            AttributeNames=["SentTimestamp", "ApproximateReceiveCount"],
        )
        return response.get("Messages", [])

    def _process(self, message: dict) -> None:
        SQS_RECEIVED.inc()
        system = message.get("Attributes", {})
        sent_ts = int(system["SentTimestamp"]) if system.get("SentTimestamp") else None
        receive_count = int(system.get("ApproximateReceiveCount", "1"))
        outcome = self._handler.handle(
            message.get("Body", ""), message.get("MessageAttributes", {}), sent_ts, receive_count
        )
        if outcome.delete_message:
            try:
                self._sqs.delete_message(
                    QueueUrl=self._settings.sqs_queue_url, ReceiptHandle=message["ReceiptHandle"]
                )
            except Exception:  # noqa: BLE001
                SQS_DELETE_ERRORS.inc()
                log.exception("sqs delete failed (message will be redelivered)")


__all__ = ["Worker", "build_sqs_client", "Outcome"]
