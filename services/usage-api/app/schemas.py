"""Request and response models."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal["data", "voice", "sms"]


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


class SubscriberDetail(SubscriberOut):
    usage: list[UsageLine]


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
