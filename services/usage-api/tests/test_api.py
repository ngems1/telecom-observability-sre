"""API integration tests: real FastAPI app, in-memory SQLite, SQS mocked with moto."""
import json

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app.config import Settings
from app.main import create_app

REGION = "us-east-1"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


@pytest.fixture
def stack(env):
    """(client, sqs, queue_url) with chaos enabled."""
    with mock_aws():
        sqs = boto3.client("sqs", region_name=REGION)
        queue_url = sqs.create_queue(QueueName="usage-alerts-test")["QueueUrl"]
        settings = Settings(
            environment="test",
            database_url="sqlite+pysqlite:///:memory:",
            aws_region=REGION,
            sqs_queue_url=queue_url,
            seed_subscribers=3,
            chaos_enabled=True,
            log_level="WARNING",
            payment_decline_rate=0.0,  # deterministic; declines are injected explicitly where tested
            payment_timeout_ms=300,
        )
        with TestClient(create_app(settings)) as client:
            yield client, sqs, queue_url


def _messages(sqs, queue_url):
    resp = sqs.receive_message(
        QueueUrl=queue_url, MaxNumberOfMessages=10, MessageAttributeNames=["All"], WaitTimeSeconds=0
    )
    return resp.get("Messages", [])


def _first_subscriber(client):
    return client.get("/v1/subscribers").json()[0]


def test_probes(stack):
    client, _, _ = stack
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["dependencies"] == {"database": "ok", "queue": "ok"}


def test_seed_data_and_plans(stack):
    client, _, _ = stack
    assert len(client.get("/v1/subscribers").json()) == 3
    assert [p["id"] for p in client.get("/v1/plans").json()] == ["basic", "plus", "max"]


def test_correlation_id_is_echoed_and_generated(stack):
    client, _, _ = stack
    echoed = client.get("/v1/plans", headers={"X-Correlation-ID": "trace-me-123"})
    assert echoed.headers["X-Correlation-ID"] == "trace-me-123"
    assert client.get("/v1/plans").headers["X-Correlation-ID"]


def test_usage_below_threshold_publishes_nothing(stack):
    client, sqs, url = stack
    sub = _first_subscriber(client)
    resp = client.post("/v1/usage", json={"subscriber_id": sub["id"], "kind": "sms", "amount": 1})
    assert resp.status_code == 201
    assert resp.json()["alerts_published"] == []
    assert _messages(sqs, url) == []


def test_crossing_80_then_100_publishes_two_events_with_correlation_id(stack):
    client, sqs, url = stack
    sub = _first_subscriber(client)  # +15550100000 -> "basic": 2048 MB
    headers = {"X-Correlation-ID": "demo-corr-1"}

    r1 = client.post("/v1/usage", json={"subscriber_id": sub["id"], "kind": "data", "amount": 1700}, headers=headers)
    assert r1.status_code == 201 and r1.json()["alerts_published"] == [80]
    r2 = client.post("/v1/usage", json={"subscriber_id": sub["id"], "kind": "data", "amount": 400}, headers=headers)
    assert r2.json()["alerts_published"] == [100]

    messages = _messages(sqs, url)
    assert len(messages) == 2
    for message in messages:
        event = json.loads(message["Body"])
        assert event["type"] == "usage.threshold_crossed"
        assert event["subscriber_id"] == sub["id"]
        assert event["correlation_id"] == "demo-corr-1"
        assert message["MessageAttributes"]["correlation_id"]["StringValue"] == "demo-corr-1"
    assert sorted(json.loads(m["Body"])["threshold_pct"] for m in messages) == [80, 100]


def test_subscriber_detail_reports_usage(stack):
    client, _, _ = stack
    sub = _first_subscriber(client)
    client.post("/v1/usage", json={"subscriber_id": sub["id"], "kind": "voice", "amount": 50})
    detail = client.get(f"/v1/subscribers/{sub['id']}").json()
    voice = next(line for line in detail["usage"] if line["kind"] == "voice")
    assert voice["used"] == 50 and voice["quota"] == 200 and voice["percent"] == 25.0


def test_validation_and_not_found(stack):
    client, _, _ = stack
    sub = _first_subscriber(client)
    assert client.post("/v1/usage", json={"subscriber_id": sub["id"], "kind": "fax", "amount": 1}).status_code == 422
    assert client.post("/v1/usage", json={"subscriber_id": sub["id"], "kind": "sms", "amount": 0}).status_code == 422
    assert client.post("/v1/usage", json={"subscriber_id": "nope", "kind": "sms", "amount": 1}).status_code == 404
    assert client.get("/v1/subscribers/nope").status_code == 404


def test_create_subscriber_and_duplicate(stack):
    client, _, _ = stack
    body = {"msisdn": "+15559990000", "name": "Test User", "plan_id": "plus"}
    assert client.post("/v1/subscribers", json=body).status_code == 201
    assert client.post("/v1/subscribers", json=body).status_code == 409
    assert client.post("/v1/subscribers", json={**body, "msisdn": "+15559990001", "plan_id": "gold"}).status_code == 404


def test_metrics_use_route_templates(stack):
    client, _, _ = stack
    sub = _first_subscriber(client)
    client.get(f"/v1/subscribers/{sub['id']}")
    text = client.get("/metrics").text
    assert 'route="/v1/subscribers/{subscriber_id}"' in text
    assert "http_request_duration_seconds_bucket" in text
    assert "db_query_duration_seconds_bucket" in text


def test_chaos_error_injection_is_visible_in_metrics(stack):
    client, _, _ = stack
    assert client.post("/chaos/errors", json={"rate": 1.0}).status_code == 200
    assert client.get("/v1/plans").status_code == 500
    assert client.get("/healthz").status_code == 200  # probes are exempt
    client.post("/chaos/reset")
    assert client.get("/v1/plans").status_code == 200
    assert 'status="500"' in client.get("/metrics").text


def test_chaos_latency_injection(stack):
    import time

    client, _, _ = stack
    client.post("/chaos/latency", json={"delay_ms": 200})
    start = time.perf_counter()
    client.get("/v1/plans")
    assert time.perf_counter() - start >= 0.19
    client.post("/chaos/reset")


def test_poison_message_lands_on_queue(stack):
    client, sqs, url = stack
    assert client.post("/chaos/poison-message?kind=malformed").status_code == 200
    bodies = [m["Body"] for m in _messages(sqs, url)]
    assert bodies == ["{this is not json"]


def test_publish_failure_does_not_lose_usage(env):
    """If the queue is unavailable the usage write still succeeds and the failure is reported."""
    with mock_aws():
        settings = Settings(
            environment="test",
            database_url="sqlite+pysqlite:///:memory:",
            aws_region=REGION,
            sqs_queue_url="https://sqs.us-east-1.amazonaws.com/123456789012/does-not-exist",
            seed_subscribers=1,
            log_level="CRITICAL",
        )
        with TestClient(create_app(settings)) as client:
            sub = client.get("/v1/subscribers").json()[0]
            resp = client.post("/v1/usage", json={"subscriber_id": sub["id"], "kind": "data", "amount": 2000})
            assert resp.status_code == 201
            assert resp.json()["alerts_published"] == []
            assert resp.json()["alerts_failed"] == [80]
            assert client.get("/readyz").json()["dependencies"]["queue"] == "degraded"
            assert client.get("/readyz").status_code == 200


# --------------------------------------------------------------------------- self-care: balance, top-ups, bundles
def _balance(client, subscriber_id):
    return client.get(f"/v1/subscribers/{subscriber_id}/balance").json()["balance_cents"]


def _topup(client, subscriber_id, amount=1000, key=None, method="card", headers=None):
    hdrs = dict(headers or {})
    if key:
        hdrs["Idempotency-Key"] = key
    return client.post(
        "/v1/topups", json={"subscriber_id": subscriber_id, "amount_cents": amount, "payment_method": method}, headers=hdrs
    )


def test_seeded_subscribers_have_an_opening_balance(stack):
    client, _, _ = stack
    sub = _first_subscriber(client)
    detail = client.get(f"/v1/subscribers/{sub['id']}").json()
    assert detail["balance_cents"] == 500 and detail["currency"] == "USD" and detail["active_bundles"] == []
    assert _balance(client, sub["id"]) == 500


def test_topup_credits_wallet_and_publishes_event(stack):
    client, sqs, url = stack
    sub = _first_subscriber(client)
    before = _balance(client, sub["id"])
    resp = _topup(client, sub["id"], 1000, key="tap-1", headers={"X-Correlation-ID": "topup-corr-1"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "succeeded" and body["balance_after_cents"] == before + 1000
    assert _balance(client, sub["id"]) == before + 1000
    events = [json.loads(m["Body"]) for m in _messages(sqs, url)]
    assert [e["type"] for e in events] == ["topup.succeeded"]
    assert events[0]["correlation_id"] == "topup-corr-1"
    assert events[0]["balance_cents"] == before + 1000 and events[0]["msisdn"] == sub["msisdn"]
    assert client.get(f"/v1/topups/{body['id']}").json()["status"] == "succeeded"


def test_double_tap_with_same_idempotency_key_charges_once(stack):
    client, sqs, url = stack
    sub = _first_subscriber(client)
    before = _balance(client, sub["id"])
    first = _topup(client, sub["id"], 2000, key="double-tap")
    second = _topup(client, sub["id"], 2000, key="double-tap")
    assert first.status_code == 201
    assert second.status_code == 200 and second.headers["Idempotent-Replayed"] == "true"
    assert second.json()["id"] == first.json()["id"]
    assert _balance(client, sub["id"]) == before + 2000
    assert len(client.get(f"/v1/subscribers/{sub['id']}/topups").json()) == 1
    assert len(_messages(sqs, url)) == 1
    assert "topup_idempotent_replays_total" in client.get("/metrics").text


def test_declined_payment_is_402_and_not_credited(stack):
    client, sqs, url = stack
    sub = _first_subscriber(client)
    before = _balance(client, sub["id"])
    client.post("/chaos/payments", json={"decline_rate": 1.0})
    resp = _topup(client, sub["id"], 1000, key="declined-1")
    client.post("/chaos/reset")
    assert resp.status_code == 402
    assert resp.json()["status"] == "declined" and resp.json()["failure_reason"] == "declined"
    assert _balance(client, sub["id"]) == before
    event = json.loads(_messages(sqs, url)[0]["Body"])
    assert event["type"] == "topup.failed" and event["reason"] == "declined"


def test_payment_provider_failures_are_5xx_and_measured(stack):
    client, _, _ = stack
    sub = _first_subscriber(client)
    before = _balance(client, sub["id"])

    client.post("/chaos/payments", json={"error_rate": 1.0})
    error = _topup(client, sub["id"], key="err-1")
    client.post("/chaos/payments", json={"latency_ms": 1000})  # above the 300 ms test timeout
    timeout = _topup(client, sub["id"], key="timeout-1")
    client.post("/chaos/reset")

    assert error.status_code == 502 and error.json()["failure_reason"] == "provider_error"
    assert timeout.status_code == 504 and timeout.json()["failure_reason"] == "provider_timeout"
    assert _balance(client, sub["id"]) == before
    metrics = client.get("/metrics").text
    assert 'payment_provider_requests_total{result="timeout"}' in metrics
    assert 'topups_total{method="card",result="failed"}' in metrics
    assert "payment_provider_duration_seconds_bucket" in metrics
    # the next top-up works again
    assert _topup(client, sub["id"], key="after-recovery").status_code == 201


def test_bundle_purchase_uses_balance_and_raises_the_allowance(stack):
    client, sqs, url = stack
    sub = _first_subscriber(client)  # basic plan: 200 voice minutes, opening balance 500
    before = _balance(client, sub["id"])
    resp = client.post(f"/v1/subscribers/{sub['id']}/bundles", json={"bundle_id": "voice-100"})
    assert resp.status_code == 201
    assert resp.json()["balance_cents"] == before - 300

    detail = client.get(f"/v1/subscribers/{sub['id']}").json()
    voice = next(line for line in detail["usage"] if line["kind"] == "voice")
    assert voice["quota"] == 300
    assert [b["bundle_id"] for b in detail["active_bundles"]] == ["voice-100"]
    assert [json.loads(m["Body"])["type"] for m in _messages(sqs, url)] == ["bundle.purchased"]

    # New allowance cycle: usage back to 0 and the bundle expires.
    client.post("/demo/reset-usage")
    detail = client.get(f"/v1/subscribers/{sub['id']}").json()
    assert next(line for line in detail["usage"] if line["kind"] == "voice")["quota"] == 200
    assert detail["active_bundles"] == []


def test_bundle_needs_enough_balance(stack):
    client, _, _ = stack
    new = client.post("/v1/subscribers", json={"msisdn": "+15559990077", "name": "New Customer"}).json()
    assert _balance(client, new["id"]) == 0
    assert client.post(f"/v1/subscribers/{new['id']}/bundles", json={"bundle_id": "data-5gb"}).status_code == 402
    assert _topup(client, new["id"], 1500, key="first-topup").status_code == 201
    assert client.post(f"/v1/subscribers/{new['id']}/bundles", json={"bundle_id": "data-5gb"}).status_code == 201
    assert _balance(client, new["id"]) == 0
    assert client.post(f"/v1/subscribers/{new['id']}/bundles", json={"bundle_id": "gold"}).status_code == 404
    assert client.post("/v1/subscribers/nope/bundles", json={"bundle_id": "data-1gb"}).status_code == 404


def test_topup_validation(stack):
    client, _, _ = stack
    sub = _first_subscriber(client)
    assert _topup(client, sub["id"], amount=50).status_code == 422             # below the minimum
    assert _topup(client, sub["id"], amount=10**6).status_code == 422          # above the maximum
    assert _topup(client, sub["id"], method="cash").status_code == 422
    assert _topup(client, sub["id"], key="not a valid key!").status_code == 422
    assert _topup(client, "nope", key="k-404").status_code == 404
    assert client.get("/v1/topups/nope").status_code == 404


def test_catalogue_and_lookup_by_phone_number(stack):
    client, _, _ = stack
    assert {b["id"] for b in client.get("/v1/bundles").json()} >= {"data-1gb", "voice-100"}
    sub = _first_subscriber(client)
    found = client.get("/v1/subscribers", params={"msisdn": sub["msisdn"]}).json()
    assert [s["id"] for s in found] == [sub["id"]]
