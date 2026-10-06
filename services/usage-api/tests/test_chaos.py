"""Failure-injection behaviour."""
import asyncio
import time
import unittest

from app.chaos import ChaosState


class ChaosStateTests(unittest.TestCase):
    def test_default_is_inert(self):
        state = ChaosState()
        self.assertIsNone(asyncio.run(state.apply()))

    def test_error_rate_one_always_fails(self):
        state = ChaosState(error_rate=1.0)
        response = asyncio.run(state.apply())
        self.assertIsNotNone(response)
        self.assertEqual(response.status_code, 500)

    def test_latency_delays_request(self):
        state = ChaosState(latency_ms=120)
        start = time.perf_counter()
        result = asyncio.run(state.apply())
        self.assertIsNone(result)
        self.assertGreaterEqual(time.perf_counter() - start, 0.11)

    def test_probability_zero_skips_latency(self):
        state = ChaosState()
        state.set_latency(2000, probability=0.0)
        start = time.perf_counter()
        asyncio.run(state.apply())
        self.assertLess(time.perf_counter() - start, 0.5)

    def test_reset_clears_everything(self):
        state = ChaosState(latency_ms=500, error_rate=0.5)
        state.reset()
        self.assertEqual(state.snapshot(), {"latency_ms": 0, "latency_probability": 1.0, "error_rate": 0.0})

    def test_values_are_clamped(self):
        state = ChaosState()
        state.set_error_rate(5)
        state.set_latency(-10, probability=3)
        self.assertEqual(state.error_rate, 1.0)
        self.assertEqual(state.latency_ms, 0)
        self.assertEqual(state.latency_probability, 1.0)


if __name__ == "__main__":
    unittest.main()
