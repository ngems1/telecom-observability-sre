"""Unit tests for the pure threshold logic (standard library only)."""
import unittest
from datetime import datetime, timezone

from app.logic import EVENT_TYPE, build_event, crossed_thresholds, percent_used


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


if __name__ == "__main__":
    unittest.main()
