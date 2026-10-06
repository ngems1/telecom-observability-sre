"""Worker integration: real SQS semantics (moto), real handler, SQLite store."""
import json
import time

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app.config import Settings
from app.main import create_app

REGION = "us-east-1"


def wait_for(predicate, timeout=10.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def event(event_id="evt-1", **overrides):
    body = {
        "event_id": event_id,
        "type": "usage.threshold_crossed",
        "subscriber_id": "sub-1",
        "msisdn": "+15550100001",
        "kind": "data",
        "threshold_pct": 80,
        "used": 1700,
        "quota": 2048,
    }
    body.update(overrides)
    return json.dumps(body)


def queue_depth(sqs, url):
    attrs = sqs.get_queue_attributes(
        QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"]
    )["Attributes"]
    return int(attrs["ApproximateNumberOfMessages"]) + int(attrs["ApproximateNumberOfMessagesNotVisible"])


@pytest.fixture
def stack(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        sqs = boto3.client("sqs", region_name=REGION)
        url = sqs.create_queue(QueueName="usage-alerts-test", Attributes={"VisibilityTimeout": "2"})["QueueUrl"]
        settings = Settings(
            environment="test",
            database_url="sqlite+pysqlite:///:memory:",
            aws_region=REGION,
            sqs_queue_url=url,
            wait_seconds=1,
            chaos_enabled=True,
            log_level="CRITICAL",
        )
        with TestClient(create_app(settings)) as client:
            yield client, sqs, url


def counter(client, result):
    """Current value of notifications_processed_total{result=...} (metrics are process-global, so tests use deltas)."""
    prefix = f'notifications_processed_total{{result="{result}"}}'
    for line in client.get("/metrics").text.splitlines():
        if line.startswith(prefix):
            return float(line.split()[-1])
    return 0.0


def test_probes(stack):
    client, _, _ = stack
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["dependencies"] == {"database": "ok", "worker": "ok"}


def test_message_is_consumed_and_deleted(stack):
    client, sqs, url = stack
    base = counter(client, "sent")
    sqs.send_message(
        QueueUrl=url,
        MessageBody=event(),
        MessageAttributes={"correlation_id": {"DataType": "String", "StringValue": "demo-corr"}},
    )
    assert wait_for(lambda: counter(client, "sent") >= base + 1)
    assert wait_for(lambda: queue_depth(sqs, url) == 0)


def test_stall_keeps_pod_healthy_but_stops_consumption(stack):
    client, sqs, url = stack
    base = counter(client, "sent")
    assert client.post("/chaos/stall", json={"enabled": True}).json()["stalled"] is True
    sqs.send_message(QueueUrl=url, MessageBody=event("evt-stall"))
    time.sleep(2.5)
    # The point of the scenario: Kubernetes sees a perfectly healthy pod ...
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    # ... while the message sits in the queue, unprocessed.
    assert counter(client, "sent") == base
    assert queue_depth(sqs, url) == 1
    # Recovery
    client.post("/chaos/stall", json={"enabled": False})
    assert wait_for(lambda: counter(client, "sent") >= base + 1)


def test_duplicate_delivery_sends_once(stack):
    client, sqs, url = stack
    base = counter(client, "sent")
    sqs.send_message(QueueUrl=url, MessageBody=event("evt-dup"))
    sqs.send_message(QueueUrl=url, MessageBody=event("evt-dup"))
    assert wait_for(lambda: queue_depth(sqs, url) == 0)
    assert counter(client, "sent") == base + 1
    assert counter(client, "duplicate") >= 1


def test_poison_message_is_not_deleted(stack):
    client, sqs, url = stack
    base = counter(client, "invalid")
    sqs.send_message(QueueUrl=url, MessageBody="{not json")
    assert wait_for(lambda: counter(client, "invalid") >= base + 1)
    assert queue_depth(sqs, url) == 1  # stays on the queue until the redrive policy moves it to the DLQ
