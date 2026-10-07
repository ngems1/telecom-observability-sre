"""Payment provider used by top-ups.

A simulated card / mobile-money gateway: realistic latency, a small natural decline rate, and failure
injection (slow provider, provider errors, extra declines) for the "external dependency degraded" scenario.
Its own metrics (payment_provider_*) are what let you say "it is the provider, not our code or database".
In a real system this class would wrap the provider's HTTP API with the same timeout and the same outcomes.
"""
from __future__ import annotations

import random
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from .observability import CHAOS_ACTIVE, PAYMENT_LATENCY, PAYMENT_REQUESTS


class PaymentDeclined(Exception):
    """The customer's bank or wallet refused the payment (a business outcome, not an outage)."""


class PaymentProviderError(Exception):
    """The provider failed (5xx, connection error)."""


class PaymentTimeout(Exception):
    """No answer within our timeout."""


@dataclass(frozen=True)
class PaymentResult:
    provider_ref: str


class SimulatedPaymentProvider:
    def __init__(
        self,
        timeout_s: float = 2.0,
        decline_rate: float = 0.02,
        base_latency_s: tuple[float, float] = (0.04, 0.18),
        rng: Callable[[], float] = random.random,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout_s = timeout_s
        self.decline_rate = decline_rate
        self.base_latency_s = base_latency_s
        self._rng = rng
        self._sleep = sleep
        # failure injection
        self.chaos_latency_ms = 0
        self.chaos_error_rate = 0.0
        self.chaos_decline_rate = 0.0
        self._publish_gauges()

    # ------------------------------------------------------------------ failure injection
    def set_chaos(self, latency_ms: int = 0, error_rate: float = 0.0, decline_rate: float = 0.0) -> None:
        self.chaos_latency_ms = max(0, latency_ms)
        self.chaos_error_rate = min(1.0, max(0.0, error_rate))
        self.chaos_decline_rate = min(1.0, max(0.0, decline_rate))
        self._publish_gauges()

    def reset_chaos(self) -> None:
        self.set_chaos(0, 0.0, 0.0)

    def chaos_snapshot(self) -> dict:
        return {
            "latency_ms": self.chaos_latency_ms,
            "error_rate": self.chaos_error_rate,
            "decline_rate": self.chaos_decline_rate,
            "timeout_ms": int(self.timeout_s * 1000),
        }

    def _publish_gauges(self) -> None:
        CHAOS_ACTIVE.labels(type="payment_latency").set(1 if self.chaos_latency_ms > 0 else 0)
        CHAOS_ACTIVE.labels(type="payment_errors").set(1 if self.chaos_error_rate > 0 else 0)
        CHAOS_ACTIVE.labels(type="payment_declines").set(1 if self.chaos_decline_rate > 0 else 0)

    # ------------------------------------------------------------------ the call
    def charge(self, amount_cents: int, method: str, idempotency_key: str) -> PaymentResult:
        """Charge the customer. Raises PaymentDeclined, PaymentProviderError or PaymentTimeout."""
        low, high = self.base_latency_s
        latency = low + (high - low) * self._rng() + self.chaos_latency_ms / 1000
        result = "error"
        started = time.perf_counter()
        try:
            if latency >= self.timeout_s:
                # We stop waiting at our timeout; the slow answer never arrives.
                self._sleep(self.timeout_s)
                result = "timeout"
                raise PaymentTimeout(f"no answer from the payment provider within {self.timeout_s:.1f}s")
            self._sleep(latency)
            if self.chaos_error_rate > 0 and self._rng() < self.chaos_error_rate:
                raise PaymentProviderError("payment provider returned 503")
            if self._rng() < max(self.decline_rate, self.chaos_decline_rate):
                result = "declined"
                raise PaymentDeclined("payment declined by the issuer")
            result = "approved"
            return PaymentResult(provider_ref=f"pay_{uuid.uuid4().hex[:16]}")
        finally:
            PAYMENT_REQUESTS.labels(result=result).inc()
            PAYMENT_LATENCY.observe(time.perf_counter() - started)
