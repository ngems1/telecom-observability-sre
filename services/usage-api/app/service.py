"""Database + messaging operations behind the HTTP routes."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .events import Publisher
from .logic import (
    BUNDLE_PURCHASED,
    BUNDLES,
    KIND_TO_QUOTA_FIELD,
    KINDS,
    TOPUP_FAILED,
    TOPUP_SUCCEEDED,
    build_event,
    crossed_thresholds,
    effective_quota,
    new_event,
    percent_used,
)
from .models import BundlePurchase, Plan, Subscriber, TopUp, UsageRecord, UsageTotal, Wallet
from .observability import (
    ALERT_EVENTS,
    BUNDLE_REVENUE,
    BUNDLES_SOLD,
    EVENTS_PUBLISHED,
    TOPUP_AMOUNT,
    TOPUP_REPLAYS,
    TOPUPS,
    USAGE_RECORDS,
)
from .payments import PaymentDeclined, PaymentProviderError, PaymentTimeout, SimulatedPaymentProvider

log = logging.getLogger(__name__)


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class InsufficientFunds(Exception):
    pass


def extra_allowance(db: Session, subscriber_id: str, kind: str) -> int:
    """Allowance added by active bundles of this kind (current cycle)."""
    total = db.scalar(
        select(func.coalesce(func.sum(BundlePurchase.amount), 0)).where(
            BundlePurchase.subscriber_id == subscriber_id,
            BundlePurchase.kind == kind,
            BundlePurchase.active.is_(True),
        )
    )
    return int(total or 0)


def quota_for(db: Session, plan: Plan, subscriber_id: str, kind: str) -> int:
    return effective_quota(getattr(plan, KIND_TO_QUOTA_FIELD[kind]), [extra_allowance(db, subscriber_id, kind)])


def get_wallet(db: Session, subscriber_id: str, *, for_update: bool = False) -> Wallet:
    """The subscriber's wallet; created empty for subscribers that predate wallets."""
    stmt = select(Wallet).where(Wallet.subscriber_id == subscriber_id)
    if for_update:
        stmt = stmt.with_for_update()
    wallet = db.scalar(stmt)
    if wallet is None:
        wallet = Wallet(subscriber_id=subscriber_id, balance_cents=0)
        db.add(wallet)
        db.flush()
    return wallet


def _publish(publisher: Publisher, event: dict, correlation_id: str) -> bool:
    """Publish after the database commit. A queue outage never fails the customer's action;
    it is logged and counted (sqs_publish_total{result="error"})."""
    try:
        publisher.publish(event, correlation_id)
    except Exception:  # noqa: BLE001
        log.exception("failed to publish event", extra={"event_type": event["type"], "event_id": event["event_id"]})
        return False
    EVENTS_PUBLISHED.labels(type=event["type"]).inc()
    return True


def create_subscriber(db: Session, msisdn: str, name: str, plan_id: str) -> Subscriber:
    if db.get(Plan, plan_id) is None:
        raise NotFoundError(f"plan '{plan_id}' not found")
    sub = Subscriber(id=str(uuid.uuid4()), msisdn=msisdn, name=name, plan_id=plan_id)
    db.add(sub)
    for kind in KINDS:
        db.add(UsageTotal(subscriber_id=sub.id, kind=kind, used=0))
    db.add(Wallet(subscriber_id=sub.id, balance_cents=0))
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
        quota = quota_for(db, plan, subscriber.id, kind)
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
    quota = quota_for(db, plan, sub.id, kind)

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
        if not _publish(publisher, event, correlation_id):  # never fail the usage write because the queue is down
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
    """Demo helper: start a new allowance cycle for everyone (usage back to 0, bundles expire)."""
    rows = db.scalars(select(UsageTotal)).all()
    for row in rows:
        row.used = 0
    for bundle in db.scalars(select(BundlePurchase).where(BundlePurchase.active.is_(True))):
        bundle.active = False
    db.commit()
    return len(rows)


# --------------------------------------------------------------------------- self-care: balance, top-ups, bundles
def get_subscriber(db: Session, subscriber_id: str) -> Subscriber:
    sub = db.get(Subscriber, subscriber_id)
    if sub is None:
        raise NotFoundError(f"subscriber '{subscriber_id}' not found")
    return sub


def active_bundles(db: Session, subscriber_id: str) -> list[BundlePurchase]:
    return list(
        db.scalars(
            select(BundlePurchase)
            .where(BundlePurchase.subscriber_id == subscriber_id, BundlePurchase.active.is_(True))
            .order_by(BundlePurchase.created_at)
        )
    )


def request_topup(
    db: Session,
    publisher: Publisher,
    provider: SimulatedPaymentProvider,
    *,
    subscriber_id: str,
    amount_cents: int,
    method: str,
    idempotency_key: str,
    currency: str,
    correlation_id: str,
) -> tuple[TopUp, bool]:
    """Charge the customer through the payment provider and credit the wallet.

    Returns (top-up, replayed). ``replayed`` is True when this idempotency key was already used by this
    subscriber: the first result is returned and nothing is charged again (double tap, client retry).

    Outcomes: succeeded (wallet credited in the same transaction), declined (customer side: HTTP 402),
    failed / provider_error or provider_timeout (our side: HTTP 502 / 504, burns the availability SLO).
    A real integration would treat a timeout as "unknown" and reconcile with the provider later; the
    simulated provider guarantees nothing was charged when it times out.
    """
    sub = get_subscriber(db, subscriber_id)

    existing = db.scalar(
        select(TopUp).where(TopUp.subscriber_id == sub.id, TopUp.idempotency_key == idempotency_key)
    )
    if existing is not None:
        TOPUP_REPLAYS.inc()
        return existing, True

    topup = TopUp(
        id=str(uuid.uuid4()),
        subscriber_id=sub.id,
        amount_cents=amount_cents,
        currency=currency,
        payment_method=method,
        idempotency_key=idempotency_key,
        status="pending",
        correlation_id=correlation_id[:64],
    )
    db.add(topup)
    try:
        db.commit()  # the pending row is the idempotency lock: a concurrent duplicate fails here
    except IntegrityError:
        db.rollback()
        TOPUP_REPLAYS.inc()
        return db.scalar(
            select(TopUp).where(TopUp.subscriber_id == sub.id, TopUp.idempotency_key == idempotency_key)
        ), True

    try:
        payment = provider.charge(amount_cents, method, idempotency_key)
    except PaymentDeclined:
        topup.status, topup.failure_reason = "declined", "declined"
    except PaymentTimeout:
        topup.status, topup.failure_reason = "failed", "provider_timeout"
    except PaymentProviderError:
        topup.status, topup.failure_reason = "failed", "provider_error"
    else:
        wallet = get_wallet(db, sub.id, for_update=True)
        wallet.balance_cents += amount_cents
        wallet.updated_at = datetime.now(timezone.utc)
        topup.status, topup.provider_ref = "succeeded", payment.provider_ref
        topup.balance_after_cents = wallet.balance_cents
    topup.completed_at = datetime.now(timezone.utc)
    db.commit()

    TOPUPS.labels(method=method, result=topup.status).inc()
    common = dict(
        subscriber_id=sub.id, msisdn=sub.msisdn, topup_id=topup.id, amount_cents=amount_cents, currency=currency
    )
    if topup.status == "succeeded":
        TOPUP_AMOUNT.labels(method=method).inc(amount_cents)
        event = new_event(TOPUP_SUCCEEDED, correlation_id, balance_cents=topup.balance_after_cents, **common)
        log.info("top-up succeeded", extra={"topup_id": topup.id, "amount_cents": amount_cents, "method": method})
    else:
        event = new_event(TOPUP_FAILED, correlation_id, reason=topup.failure_reason, **common)
        log.warning(
            "top-up not completed",
            extra={"topup_id": topup.id, "reason": topup.failure_reason, "method": method},
        )
    _publish(publisher, event, correlation_id)
    return topup, False


def list_topups(db: Session, subscriber_id: str, limit: int = 20) -> list[TopUp]:
    get_subscriber(db, subscriber_id)
    return list(
        db.scalars(
            select(TopUp).where(TopUp.subscriber_id == subscriber_id).order_by(TopUp.created_at.desc()).limit(limit)
        )
    )


def purchase_bundle(
    db: Session, publisher: Publisher, *, subscriber_id: str, bundle_id: str, currency: str, correlation_id: str
) -> dict:
    """Pay for an add-on bundle from the prepaid balance; the allowance applies immediately."""
    bundle = BUNDLES.get(bundle_id)
    if bundle is None:
        raise NotFoundError(f"bundle '{bundle_id}' not found")
    sub = get_subscriber(db, subscriber_id)
    wallet = get_wallet(db, sub.id, for_update=True)
    if wallet.balance_cents < bundle["price_cents"]:
        balance = wallet.balance_cents
        db.rollback()
        raise InsufficientFunds(
            f"balance {balance} cents is lower than the bundle price {bundle['price_cents']} cents"
        )
    wallet.balance_cents -= bundle["price_cents"]
    wallet.updated_at = datetime.now(timezone.utc)
    purchase = BundlePurchase(
        subscriber_id=sub.id,
        bundle_id=bundle_id,
        kind=bundle["kind"],
        amount=bundle["amount"],
        price_cents=bundle["price_cents"],
        correlation_id=correlation_id[:64],
    )
    db.add(purchase)
    db.commit()

    BUNDLES_SOLD.labels(bundle_id=bundle_id).inc()
    BUNDLE_REVENUE.labels(bundle_id=bundle_id).inc(bundle["price_cents"])
    log.info("bundle purchased", extra={"bundle_id": bundle_id, "subscriber_id": sub.id})
    _publish(
        publisher,
        new_event(
            BUNDLE_PURCHASED,
            correlation_id,
            subscriber_id=sub.id,
            msisdn=sub.msisdn,
            bundle_id=bundle_id,
            bundle_name=bundle["name"],
            kind=bundle["kind"],
            amount=bundle["amount"],
            price_cents=bundle["price_cents"],
            currency=currency,
            balance_cents=wallet.balance_cents,
        ),
        correlation_id,
    )
    return {
        "purchase_id": purchase.id,
        "subscriber_id": sub.id,
        "bundle_id": bundle_id,
        "kind": bundle["kind"],
        "amount": bundle["amount"],
        "price_cents": bundle["price_cents"],
        "balance_cents": wallet.balance_cents,
        "currency": currency,
    }
