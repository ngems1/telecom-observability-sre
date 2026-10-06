"""Failure injection for the Week 4 failure scenarios.

Only wired into the app when CHAOS_ENABLED=true (never in prod values). The
control endpoints are intentionally not routed by the Ingress; use
``kubectl port-forward`` or ``kubectl exec`` to reach them.
"""
from __future__ import annotations

import asyncio
import random

from starlette.responses import JSONResponse

from .observability import CHAOS_ACTIVE


class ChaosState:
    def __init__(
        self,
        latency_ms: int = 0,
        latency_probability: float = 1.0,
        error_rate: float = 0.0,
    ) -> None:
        self.latency_ms = latency_ms
        self.latency_probability = latency_probability
        self.error_rate = error_rate
        self._publish_gauges()

    def _publish_gauges(self) -> None:
        CHAOS_ACTIVE.labels(type="latency").set(1 if self.latency_ms > 0 else 0)
        CHAOS_ACTIVE.labels(type="errors").set(1 if self.error_rate > 0 else 0)

    def set_latency(self, delay_ms: int, probability: float = 1.0) -> None:
        self.latency_ms = max(0, delay_ms)
        self.latency_probability = min(1.0, max(0.0, probability))
        self._publish_gauges()

    def set_error_rate(self, rate: float) -> None:
        self.error_rate = min(1.0, max(0.0, rate))
        self._publish_gauges()

    def reset(self) -> None:
        self.latency_ms = 0
        self.latency_probability = 1.0
        self.error_rate = 0.0
        self._publish_gauges()

    def snapshot(self) -> dict:
        return {
            "latency_ms": self.latency_ms,
            "latency_probability": self.latency_probability,
            "error_rate": self.error_rate,
        }

    async def apply(self) -> JSONResponse | None:
        """Delay and/or fail the current request. Returns a response to short-circuit."""
        if self.latency_ms > 0 and random.random() < self.latency_probability:
            await asyncio.sleep(self.latency_ms / 1000)
        if self.error_rate > 0 and random.random() < self.error_rate:
            return JSONResponse({"detail": "chaos: injected error"}, status_code=500)
        return None
