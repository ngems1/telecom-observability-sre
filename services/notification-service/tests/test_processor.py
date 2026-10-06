"""Unit tests for message handling, using in-memory fakes (no AWS, no database)."""
import json
import time
import unittest

from app.chaos import DeliveryError
from app.processor import InvalidEvent, MessageHandler, Outcome, parse_event, render_message


def make_event(**overrides):
    event = {
        "event_id": "evt-1",
        "type": "usage.threshold_crossed",
        "subscriber_id": "sub-1",
        "msisdn": "+15550100001",
        "kind": "data",
        "threshold_pct": 80,
        "used": 1700,
        "quota": 2048,
        "correlation_id": "corr-from-body",
    }
    event.update(overrides)
    return event


class FakeStore:
    def __init__(self):
        self.sent = {}

    def already_sent(self, event_id):
        return event_id in self.sent

    def record_sent(self, event, message, correlation_id, queue_delay):
        if event["event_id"] in self.sent:
            return False
        self.sent[event["event_id"]] = (message, correlation_id, queue_delay)
        return True


class FakeNotifier:
    def __init__(self, fail=False):
        self.fail = fail
        self.delivered = []

    def deliver(self, msisdn, text):
        if self.fail:
            raise DeliveryError("boom")
        self.delivered.append((msisdn, text))


class ParseTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_event(json.dumps(make_event()))["event_id"], "evt-1")

    def test_malformed_json(self):
        with self.assertRaises(InvalidEvent):
            parse_event("{not json")

    def test_not_an_object(self):
        with self.assertRaises(InvalidEvent):
            parse_event("[1, 2, 3]")

    def test_missing_fields(self):
        with self.assertRaises(InvalidEvent) as ctx:
            parse_event('{"type": "usage.threshold_crossed"}')
        self.assertIn("event_id", str(ctx.exception))

    def test_unknown_kind(self):
        with self.assertRaises(InvalidEvent):
            parse_event(json.dumps(make_event(kind="fax")))


class RenderTests(unittest.TestCase):
    def test_threshold_message(self):
        self.assertIn("80%", render_message(make_event()))

    def test_exhausted_message(self):
        text = render_message(make_event(threshold_pct=100, used=2100))
        self.assertIn("all of your", text)


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.notifier = FakeNotifier()
        self.handler = MessageHandler(self.store, self.notifier)

    def attrs(self, cid="corr-1"):
        return {"correlation_id": {"DataType": "String", "StringValue": cid}}

    def test_happy_path_sends_and_records(self):
        outcome = self.handler.handle(json.dumps(make_event()), self.attrs(), int(time.time() * 1000) - 3000)
        self.assertEqual(outcome, Outcome.SENT)
        self.assertTrue(outcome.delete_message)
        self.assertEqual(len(self.notifier.delivered), 1)
        _, correlation_id, delay = self.store.sent["evt-1"]
        self.assertEqual(correlation_id, "corr-1")  # message attribute wins
        self.assertGreaterEqual(delay, 2.9)

    def test_correlation_id_falls_back_to_event_body(self):
        self.handler.handle(json.dumps(make_event()), {}, None)
        self.assertEqual(self.store.sent["evt-1"][1], "corr-from-body")

    def test_duplicate_is_acknowledged_without_resending(self):
        body = json.dumps(make_event())
        self.handler.handle(body, self.attrs())
        outcome = self.handler.handle(body, self.attrs())
        self.assertEqual(outcome, Outcome.DUPLICATE)
        self.assertTrue(outcome.delete_message)
        self.assertEqual(len(self.notifier.delivered), 1)

    def test_invalid_message_is_not_deleted(self):
        outcome = self.handler.handle("{garbage", self.attrs())
        self.assertEqual(outcome, Outcome.INVALID)
        self.assertFalse(outcome.delete_message)  # SQS retries, then moves it to the DLQ

    def test_delivery_failure_is_retried_not_recorded(self):
        self.notifier.fail = True
        outcome = self.handler.handle(json.dumps(make_event()), self.attrs())
        self.assertEqual(outcome, Outcome.DELIVERY_FAILED)
        self.assertFalse(outcome.delete_message)
        self.assertEqual(self.store.sent, {})

    def test_unexpected_exception_becomes_error_outcome(self):
        class BrokenStore(FakeStore):
            def already_sent(self, event_id):
                raise RuntimeError("database down")

        handler = MessageHandler(BrokenStore(), self.notifier)
        outcome = handler.handle(json.dumps(make_event()), self.attrs())
        self.assertEqual(outcome, Outcome.ERROR)
        self.assertFalse(outcome.delete_message)


if __name__ == "__main__":
    unittest.main()
