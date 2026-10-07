"""Unit tests for message handling, using in-memory fakes (no AWS, no database)."""
import json
import time
import unittest

from app.chaos import DeliveryError
from app.processor import InvalidEvent, MessageHandler, Outcome, notification_kind, parse_event, render_message


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



def topup_event(**overrides):
    event = {
        "event_id": "evt-t1",
        "type": "topup.succeeded",
        "subscriber_id": "sub-1",
        "msisdn": "+15550100001",
        "topup_id": "top-1",
        "amount_cents": 1000,
        "currency": "USD",
        "balance_cents": 2350,
    }
    event.update(overrides)
    return event


class SelfCareEventTests(unittest.TestCase):
    def test_topup_succeeded_message(self):
        event = parse_event(json.dumps(topup_event()))
        self.assertEqual(render_message(event), "Top-up successful: $10.00 added. Your balance is now $23.50.")
        self.assertEqual(notification_kind(event), "topup")

    def test_topup_declined_and_failed_messages(self):
        declined = topup_event(type="topup.failed", reason="declined")
        del declined["balance_cents"]
        self.assertIn("declined by your bank", render_message(parse_event(json.dumps(declined))))
        failed = dict(declined, reason="provider_timeout")
        self.assertIn("could not be completed", render_message(parse_event(json.dumps(failed))))
        self.assertIn("not been charged", render_message(failed))

    def test_bundle_purchased_message(self):
        event = topup_event(type="bundle.purchased", bundle_name="1 GB data", price_cents=500, balance_cents=1850)
        text = render_message(parse_event(json.dumps(event)))
        self.assertEqual(text, "You bought 1 GB data for $5.00. Remaining balance: $18.50.")
        self.assertEqual(notification_kind(event), "bundle")

    def test_usage_kind_is_kept(self):
        self.assertEqual(notification_kind(make_event()), "data")

    def test_unknown_event_type_is_invalid(self):
        with self.assertRaises(InvalidEvent):
            parse_event(json.dumps(topup_event(type="refund.issued")))

    def test_topup_event_missing_its_fields_is_invalid(self):
        event = topup_event()
        del event["amount_cents"]
        with self.assertRaises(InvalidEvent) as ctx:
            parse_event(json.dumps(event))
        self.assertIn("amount_cents", str(ctx.exception))

    def test_handler_sends_topup_notification(self):
        store, notifier = FakeStore(), FakeNotifier()
        outcome = MessageHandler(store, notifier).handle(json.dumps(topup_event()), {}, None)
        self.assertEqual(outcome, Outcome.SENT)
        self.assertEqual(notifier.delivered[0][0], "+15550100001")
        self.assertIn("Top-up successful", notifier.delivered[0][1])


if __name__ == "__main__":
    unittest.main()
