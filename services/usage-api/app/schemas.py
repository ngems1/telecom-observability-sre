"""Request and response models."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .logic import TOPUP_MAX_CENTS, TOPUP_MIN_CENTS

Kind = Literal["data", "voice", "sms"]
PaymentMethod = Literal["card", "mobile_money", "voucher"]


class SubscriberIn(BaseModel):
    msisdn: str = Field(pattern=r"^\+?[0-9]{8,15}$", examples=["+15551234567"])
    name: str = Field(min_length=1, max_length=128)
    plan_id: str = Field(default="basic", max_length=32)


class UsageIn(BaseModel):
    subscriber_id: str = Field(min_length=1, max_length=36)
    kind: Kind
    amount: int = Field(gt=0, le=1_000_000, description="MB for data, minutes for voice, count for sms")


class UsageLine(BaseModel):
    kind: Kind
    used: int
    quota: int
    percent: float


class SubscriberOut(BaseModel):
    id: str
    msisdn: str
    name: str
    plan_id: str


class ActiveBundle(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    bundle_id: str
    kind: Kind
    amount: int
    created_at: datetime


class SubscriberDetail(SubscriberOut):
    """The self-care home screen: plan usage (including bundles), balance and active bundles."""

    usage: list[UsageLine]
    balance_cents: int = 0
    currency: str = "USD"
    active_bundles: list[ActiveBundle] = []


class PlanOut(BaseModel):
    id: str
    name: str
    data_mb: int
    voice_min: int
    sms: int
    monthly_price_cents: int


class UsageOut(BaseModel):
    record_id: int
    subscriber_id: str
    kind: Kind
    used: int
    quota: int
    percent: float
    alerts_published: list[int]
    alerts_failed: list[int]


# --------------------------------------------------------------------------- self-care
class BalanceOut(BaseModel):
    subscriber_id: str
    balance_cents: int
    currency: str


class TopUpIn(BaseModel):
    subscriber_id: str = Field(min_length=1, max_length=36)
    amount_cents: int = Field(ge=TOPUP_MIN_CENTS, le=TOPUP_MAX_CENTS, examples=[1000])
    payment_method: PaymentMethod = "card"


class TopUpOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    subscriber_id: str
    amount_cents: int
    currency: str
    payment_method: str
    status: Literal["pending", "succeeded", "declined", "failed"]
    failure_reason: str | None = None
    balance_after_cents: int | None = None
    created_at: datetime
    completed_at: datetime | None = None


class BundleOut(BaseModel):
    id: str
    name: str
    kind: Kind
    amount: int
    price_cents: int


class BundlePurchaseIn(BaseModel):
    bundle_id: str = Field(min_length=1, max_length=32, examples=["data-1gb"])


class BundlePurchaseOut(BaseModel):
    purchase_id: int
    subscriber_id: str
    bundle_id: str
    kind: Kind
    amount: int
    price_cents: int
    balance_cents: int
    currency: str
