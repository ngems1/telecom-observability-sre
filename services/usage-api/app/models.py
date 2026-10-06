"""SQLAlchemy models."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
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
