"""Database + messaging operations behind the HTTP routes."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .events import Publisher
from .logic import KIND_TO_QUOTA_FIELD, KINDS, build_event, crossed_thresholds, percent_used
from .models import Plan, Subscriber, UsageRecord, UsageTotal
from .observability import ALERT_EVENTS, USAGE_RECORDS

log = logging.getLogger(__name__)


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


def create_subscriber(db: Session, msisdn: str, name: str, plan_id: str) -> Subscriber:
    if db.get(Plan, plan_id) is None:
        raise NotFoundError(f"plan '{plan_id}' not found")
    sub = Subscriber(id=str(uuid.uuid4()), msisdn=msisdn, name=name, plan_id=plan_id)
    db.add(sub)
    for kind in KINDS:
        db.add(UsageTotal(subscriber_id=sub.id, kind=kind, used=0))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(f"msisdn {msisdn} already exists") from exc
    return sub


def usage_summary(db: Session, subscriber: Subscriber) -> list[dict]:
    plan = db.get(Plan, subscriber.plan_id)
    totals = {
        t.kind: t.used
        for t in db.scalars(select(UsageTotal).where(UsageTotal.subscriber_id == subscriber.id))
    }
    lines = []
    for kind in KINDS:
        quota = getattr(plan, KIND_TO_QUOTA_FIELD[kind])
        used = totals.get(kind, 0)
        lines.append({"kind": kind, "used": used, "quota": quota, "percent": percent_used(used, quota)})
    return lines


def record_usage(
    db: Session,
    publisher: Publisher,
    thresholds: tuple[int, ...],
    subscriber_id: str,
    kind: str,
    amount: int,
    correlation_id: str,
) -> dict:
    """Record usage, then publish one event per threshold crossed.

    Known limitation (documented in the README): the database commit and the
    SQS publish are two separate writes. If the publish fails the usage is
    kept and the failure is surfaced in ``alerts_failed`` and in the
    ``sqs_publish_total{result="error"}`` metric. A transactional outbox is the
    production fix.
    """
    sub = db.get(Subscriber, subscriber_id)
    if sub is None:
        raise NotFoundError(f"subscriber '{subscriber_id}' not found")
    plan = db.get(Plan, sub.plan_id)
    quota = getattr(plan, KIND_TO_QUOTA_FIELD[kind])

    total = db.scalar(
        select(UsageTotal)
        .where(UsageTotal.subscriber_id == sub.id, UsageTotal.kind == kind)
        .with_for_update()
    )
    if total is None:
        total = UsageTotal(subscriber_id=sub.id, kind=kind, used=0)
        db.add(total)
        db.flush()

    previous = total.used
    total.used = previous + amount
    total.updated_at = datetime.now(timezone.utc)
    record = UsageRecord(
        subscriber_id=sub.id, kind=kind, amount=amount, correlation_id=correlation_id[:64]
    )
    db.add(record)
    db.commit()

    USAGE_RECORDS.labels(kind=kind).inc()

    published: list[int] = []
    failed: list[int] = []
    for pct in crossed_thresholds(previous, total.used, quota, thresholds):
        event = build_event(
            subscriber_id=sub.id,
            msisdn=sub.msisdn,
            kind=kind,
            threshold_pct=pct,
            used=total.used,
            quota=quota,
            correlation_id=correlation_id,
        )
        try:
            publisher.publish(event, correlation_id)
        except Exception:  # noqa: BLE001 - never fail the usage write because the queue is down
            log.exception(
                "failed to publish usage alert",
                extra={"subscriber_id": sub.id, "kind": kind, "threshold_pct": pct},
            )
            failed.append(pct)
            continue
        ALERT_EVENTS.labels(kind=kind, threshold=str(pct)).inc()
        published.append(pct)
        log.info(
            "usage alert published",
            extra={"subscriber_id": sub.id, "kind": kind, "threshold_pct": pct, "event_id": event["event_id"]},
        )

    return {
        "record_id": record.id,
        "subscriber_id": sub.id,
        "kind": kind,
        "used": total.used,
        "quota": quota,
        "percent": percent_used(total.used, quota),
        "alerts_published": published,
        "alerts_failed": failed,
    }


def reset_usage(db: Session) -> int:
    """Demo helper: start a new allowance cycle for everyone."""
    rows = db.scalars(select(UsageTotal)).all()
    for row in rows:
        row.used = 0
    db.commit()
    return len(rows)
