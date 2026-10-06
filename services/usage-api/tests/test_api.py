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
