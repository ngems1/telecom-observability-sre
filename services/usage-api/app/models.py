"""SQLAlchemy models."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    data_mb: Mapped[int] = mapped_column(Integer)
    voice_min: Mapped[int] = mapped_column(Integer)
    sms: Mapped[int] = mapped_column(Integer)
    monthly_price_cents: Mapped[int] = mapped_column(Integer)


class Subscriber(Base):
    __tablename__ = "subscribers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    msisdn: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class UsageTotal(Base):
    """Running total per subscriber and usage kind for the current cycle."""

    __tablename__ = "usage_totals"

    subscriber_id: Mapped[str] = mapped_column(ForeignKey("subscribers.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), primary_key=True)
    used: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class UsageRecord(Base):
    """Append-only usage ledger."""

    __tablename__ = "usage_records"
    __table_args__ = (Index("ix_usage_records_subscriber", "subscriber_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscriber_id: Mapped[str] = mapped_column(ForeignKey("subscribers.id"))
    kind: Mapped[str] = mapped_column(String(8))
    amount: Mapped[int] = mapped_column(Integer)
    correlation_id: Mapped[str] = mapped_column(String(64), default="-")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# --------------------------------------------------------------------------- self-care
# New tables only (no change to the tables above), so an existing database upgrades in place:
# create_all() adds them at startup.


class Wallet(Base):
    """Prepaid airtime balance."""

    __tablename__ = "wallets"

    subscriber_id: Mapped[str] = mapped_column(ForeignKey("subscribers.id"), primary_key=True)
    balance_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class TopUp(Base):
    """One top-up request. (subscriber_id, idempotency_key) is unique: a retried or double-tapped
    request returns the first result instead of charging the customer twice."""

    __tablename__ = "topups"
    __table_args__ = (
        UniqueConstraint("subscriber_id", "idempotency_key", name="uq_topups_idempotency"),
        Index("ix_topups_subscriber", "subscriber_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    subscriber_id: Mapped[str] = mapped_column(ForeignKey("subscribers.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    payment_method: Mapped[str] = mapped_column(String(16))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|succeeded|declined|failed
    failure_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    balance_after_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), default="-")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BundlePurchase(Base):
    """Add-on allowance bought from the wallet. Active until the next allowance cycle."""

    __tablename__ = "bundle_purchases"
    __table_args__ = (Index("ix_bundles_subscriber_active", "subscriber_id", "active"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscriber_id: Mapped[str] = mapped_column(ForeignKey("subscribers.id"))
    bundle_id: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(8))
    amount: Mapped[int] = mapped_column(Integer)
    price_cents: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    correlation_id: Mapped[str] = mapped_column(String(64), default="-")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
