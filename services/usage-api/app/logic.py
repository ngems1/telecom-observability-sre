"""Pure business logic (no I/O) so it is trivial to unit test."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Iterable

KINDS = ("data", "voice", "sms")

# Plan column that holds the allowance for each usage kind.
KIND_TO_QUOTA_FIELD = {"data": "data_mb", "voice": "voice_min", "sms": "sms"}

EVENT_TYPE = "usage.threshold_crossed"


def crossed_thresholds(
    previous: int, new: int, quota: int, thresholds: Iterable[int] = (80, 100)
) -> list[int]:
    """Return the percentage thresholds crossed by moving ``previous`` -> ``new``.

    Integer arithmetic only, so 80% of 2048 MB is evaluated exactly. A threshold
    is "crossed" when usage was strictly below it before and is at or above it
    now. Each threshold therefore fires once per allowance cycle.
    """
    if quota <= 0 or new <= previous:
        return []
    crossed = []
    for pct in sorted(set(thresholds)):
        if previous * 100 < quota * pct <= new * 100:
            crossed.append(pct)
    return crossed


def percent_used(used: int, quota: int) -> float:
    if quota <= 0:
        return 0.0
    return round(used * 100 / quota, 1)


def build_event(
    *,
    subscriber_id: str,
    msisdn: str,
    kind: str,
    threshold_pct: int,
    used: int,
    quota: int,
    correlation_id: str,
    now: datetime | None = None,
) -> dict:
    """Build the ``usage.threshold_crossed`` event published to SQS."""
    return {
        "event_id": str(uuid.uuid4()),
        "type": EVENT_TYPE,
        "version": 1,
        "subscriber_id": subscriber_id,
        "msisdn": msisdn,
        "kind": kind,
        "threshold_pct": threshold_pct,
        "used": used,
        "quota": quota,
        "correlation_id": correlation_id,
        "occurred_at": (now or datetime.now(timezone.utc)).isoformat(),
    }
