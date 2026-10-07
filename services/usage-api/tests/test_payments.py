"""Unit tests for the simulated payment provider (standard library + prometheus-client only)."""
import unittest

from app.payments import PaymentDeclined, PaymentProviderError, PaymentTimeout, SimulatedPaymentProvider


class FakeClock:
    def __init__(self):
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)


def provider(rolls, **kwargs):
    """Provider whose random draws come from `rolls` (in order) and whose sleeps are recorded."""
    clock = FakeClock()
    seq = iter(rolls)
    p = SimulatedPaymentProvider(rng=lambda: next(seq), sleep=clock.sleep, **kwargs)
    return p, clock


class PaymentProviderTests(unittest.TestCase):
    def test_approved_payment_returns_a_reference(self):
        p, clock = provider([0.5, 0.9], decline_rate=0.02)  # latency draw, decline draw
        result = p.charge(1000, "card", "k1")
        self.assertTrue(result.provider_ref.startswith("pay_"))
        self.assertEqual(len(clock.slept), 1)
        self.assertLess(clock.slept[0], 0.2)

    def test_natural_decline(self):
        p, _ = provider([0.5, 0.01], decline_rate=0.02)
        with self.assertRaises(PaymentDeclined):
            p.charge(1000, "card", "k1")

    def test_injected_errors(self):
        p, _ = provider([0.5, 0.1], decline_rate=0.0)
        p.set_chaos(error_rate=0.5)
        with self.assertRaises(PaymentProviderError):
            p.charge(1000, "mobile_money", "k1")

    def test_latency_above_timeout_times_out_after_the_timeout_only(self):
        p, clock = provider([0.5], timeout_s=1.0)
        p.set_chaos(latency_ms=5000)
        with self.assertRaises(PaymentTimeout):
            p.charge(1000, "card", "k1")
        self.assertEqual(clock.slept, [1.0])  # we stop waiting at our timeout, not after 5 s

    def test_injected_latency_below_timeout_still_succeeds(self):
        p, clock = provider([0.0, 0.9], timeout_s=2.0, decline_rate=0.0)
        p.set_chaos(latency_ms=800)
        p.charge(1000, "card", "k1")
        self.assertAlmostEqual(clock.slept[0], 0.04 + 0.8, places=3)

    def test_injected_declines_override_the_natural_rate(self):
        p, _ = provider([0.5, 0.3], decline_rate=0.02)
        p.set_chaos(decline_rate=0.5)
        with self.assertRaises(PaymentDeclined):
            p.charge(1000, "card", "k1")

    def test_reset_clears_injections(self):
        p, _ = provider([])
        p.set_chaos(latency_ms=100, error_rate=1.0, decline_rate=1.0)
        p.reset_chaos()
        self.assertEqual(p.chaos_snapshot(), {"latency_ms": 0, "error_rate": 0.0, "decline_rate": 0.0, "timeout_ms": 2000})


if __name__ == "__main__":
    unittest.main()
