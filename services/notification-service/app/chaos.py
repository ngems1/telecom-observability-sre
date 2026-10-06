"""Failure injection for the Notification service.

``stalled`` is the "stuck queue processing" scenario: the pod stays healthy and
Ready, but stops consuming from SQS. Pod-level checks stay green while the
queue ages, which is why the alert has to come from queue metrics.
"""
from __future__ import annotations

import random
import time

from .observability import CHAOS_ACTIVE, WORKER_STALLED


class DeliveryError(Exception):
    """The (simulated) SMS provider rejected the message."""


class WorkerChaos:
    def __init__(
        self, stalled: bool = False, failure_rate: float = 0.0, delivery_latency_ms: int = 0
    ) -> None:
        self.stalled = stalled
        self.failure_rate = failure_rate
        self.delivery_latency_ms = delivery_latency_ms
        self._publish_gauges()

    def _publish_gauges(self) -> None:
        WORKER_STALLED.set(1 if self.stalled else 0)
        CHAOS_ACTIVE.labels(type="stall").set(1 if self.stalled else 0)
        CHAOS_ACTIVE.labels(type="delivery_failures").set(1 if self.failure_rate > 0 else 0)
        CHAOS_ACTIVE.labels(type="delivery_latency").set(1 if self.delivery_latency_ms > 0 else 0)

    def set_stalled(self, stalled: bool) -> None:
        self.stalled = stalled
        self._publish_gauges()

    def set_failure_rate(self, rate: float) -> None:
        self.failure_rate = min(1.0, max(0.0, rate))
        self._publish_gauges()

    def set_delivery_latency(self, ms: int) -> None:
        self.delivery_latency_ms = max(0, ms)
        self._publish_gauges()

    def reset(self) -> None:
        self.stalled = False
        self.failure_rate = 0.0
        self.delivery_latency_ms = 0
        self._publish_gauges()

    def snapshot(self) -> dict:
        return {
            "stalled": self.stalled,
            "failure_rate": self.failure_rate,
            "delivery_latency_ms": self.delivery_latency_ms,
        }


class SimulatedNotifier:
    """Stand-in for an SMS/push provider (e.g. SNS or Pinpoint in a real system)."""

    def __init__(self, chaos: WorkerChaos) -> None:
        self._chaos = chaos

    def deliver(self, msisdn: str, text: str) -> None:
        if self._chaos.delivery_latency_ms > 0:
            time.sleep(self._chaos.delivery_latency_ms / 1000)
        if self._chaos.failure_rate > 0 and random.random() < self._chaos.failure_rate:
            raise DeliveryError("simulated provider failure")
