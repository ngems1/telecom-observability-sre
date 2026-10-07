"""Unit tests for the pure threshold logic (standard library only)."""
import unittest
from datetime import datetime, timezone

from app.logic import (
    BUNDLES,
    EVENT_TYPE,
    TOPUP_SUCCEEDED,
    build_event,
    crossed_thresholds,
    effective_quota,
    format_money,
    new_event,
    percent_used,
)


class CrossedThresholdsTests(unittest.TestCase):
    def test_no_crossing_below_first_threshold(self):
        self.assertEqual(crossed_thresholds(0, 1000, 2048), [])

    def test_crossing_80_percent(self):
        # 80% of 2048 MB is 1638.4 -> 1639 MB is the first value at/over it.
        self.assertEqual(crossed_thresholds(1600, 1700, 2048), [80])

    def test_exact_boundary_counts_as_crossed(self):
        self.assertEqual(crossed_thresholds(0, 80, 100), [80])
        self.assertEqual(crossed_thresholds(79, 80, 100), [80])

    def test_already_at_threshold_does_not_refire(self):
        self.assertEqual(crossed_thresholds(80, 90, 100), [])

    def test_single_jump_crosses_both(self):
        self.assertEqual(crossed_thresholds(0, 120, 100), [80, 100])

    def test_crossing_100_only(self):
        self.assertEqual(crossed_thresholds(90, 100, 100), [100])

    def test_over_quota_stays_quiet_after_100(self):
        self.assertEqual(crossed_thresholds(100, 150, 100), [])

    def test_zero_quota_or_no_increase(self):
        self.assertEqual(crossed_thresholds(0, 10, 0), [])
        self.assertEqual(crossed_thresholds(50, 50, 100), [])
        self.assertEqual(crossed_thresholds(50, 40, 100), [])

    def test_custom_thresholds(self):
        self.assertEqual(crossed_thresholds(0, 60, 100, thresholds=(50, 90)), [50])
        self.assertEqual(crossed_thresholds(0, 95, 100, thresholds=(90, 50)), [50, 90])


class HelpersTests(unittest.TestCase):
    def test_percent_used(self):
        self.assertEqual(percent_used(512, 2048), 25.0)
        self.assertEqual(percent_used(5, 0), 0.0)

    def test_build_event_shape(self):
        now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
        event = build_event(
            subscriber_id="sub-1",
            msisdn="+15550100001",
            kind="data",
            threshold_pct=80,
            used=1700,
            quota=2048,
            correlation_id="abc",
            now=now,
        )
        self.assertEqual(event["type"], EVENT_TYPE)
        self.assertEqual(event["threshold_pct"], 80)
        self.assertEqual(event["correlation_id"], "abc")
        self.assertEqual(event["occurred_at"], now.isoformat())
        self.assertTrue(event["event_id"])



class SelfCareLogicTests(unittest.TestCase):
    def test_effective_quota_adds_active_bundles(self):
        self.assertEqual(effective_quota(2048, []), 2048)
        self.assertEqual(effective_quota(2048, [1024, 5120]), 8192)
        self.assertEqual(effective_quota(200, [0, -5]), 200)  # nothing negative sneaks in

    def test_bundle_raises_quota_so_thresholds_can_fire_again(self):
        # 1700 / 2048 MB was past 80%; after a 1 GB bundle the quota is 3072 and 80% is 2457.6 MB.
        quota = effective_quota(2048, [BUNDLES["data-1gb"]["amount"]])
        self.assertEqual(crossed_thresholds(1700, 2000, quota), [])
        self.assertEqual(crossed_thresholds(2000, 2500, quota), [80])

    def test_bundle_catalogue_is_consistent(self):
        for bundle_id, b in BUNDLES.items():
            self.assertIn(b["kind"], ("data", "voice", "sms"), bundle_id)
            self.assertGreater(b["amount"], 0)
            self.assertGreater(b["price_cents"], 0)
            self.assertLessEqual(len(bundle_id), 32)

    def test_format_money(self):
        self.assertEqual(format_money(1050), "$10.50")
        self.assertEqual(format_money(123456, "EUR"), "€1,234.56")
        self.assertEqual(format_money(500, "XOF"), "5.00 XOF")

    def test_new_event_envelope(self):
        event = new_event(TOPUP_SUCCEEDED, "corr-9", subscriber_id="s1", amount_cents=1000)
        self.assertEqual(event["type"], "topup.succeeded")
        self.assertEqual(event["correlation_id"], "corr-9")
        self.assertEqual(event["amount_cents"], 1000)
        self.assertEqual(event["version"], 1)
        self.assertTrue(event["event_id"] and event["occurred_at"])


if __name__ == "__main__":
    unittest.main()
