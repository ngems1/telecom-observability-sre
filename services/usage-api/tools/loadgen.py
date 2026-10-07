#!/usr/bin/env python3
"""Traffic generator for demos and failure tests (standard library only).

Simulates a normal day of a telecom self-care app, so every layer has realistic traffic:
  * customers opening the app: account + usage, balance, plans/bundles   -> API + database
  * network usage records (data / voice / SMS)                            -> API + database (+ SQS on thresholds)
  * top-ups by card / mobile money / voucher, some double taps (same key) -> API + payment provider + DB + SQS
  * bundle purchases from the balance (some fail for lack of credit: 402) -> API + database + SQS
  * "bursts": new subscriber pushed past 80% and 100% of its data allowance
                                                                          -> API + database + SQS + notifications

Examples:
  python tools/loadgen.py --base-url http://localhost:8080 --rps 5
  python tools/loadgen.py --base-url http://usage-api:8080 --rps 10 --duration 300
"""
from __future__ import annotations

import argparse
import json
import random
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter

BASIC_DATA_QUOTA_MB = 2048  # plan "basic"
TOPUP_AMOUNTS_CENTS = (500, 1000, 1000, 2000, 2000, 5000)
PAYMENT_METHODS = (("card", 0.6), ("mobile_money", 0.3), ("voucher", 0.1))
BUNDLE_IDS = ("data-1gb", "data-1gb", "data-5gb", "voice-100", "sms-200")


class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.latencies: list[float] = []
        self.statuses: Counter = Counter()

    def add(self, status: int, seconds: float) -> None:
        with self.lock:
            self.latencies.append(seconds)
            self.statuses[status] += 1

    def drain(self) -> tuple[list[float], Counter]:
        with self.lock:
            lat, st = self.latencies, self.statuses
            self.reset()
        return lat, st


def call(
    base: str, method: str, path: str, body: dict | None, stats: Stats, timeout: float, headers: dict | None = None
) -> tuple[int, dict | None]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Correlation-ID": f"loadgen-{uuid.uuid4().hex[:12]}",
            **(headers or {}),
        },
    )
    start = time.perf_counter()
    status, payload = 0, None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
            payload = json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception:  # noqa: BLE001 - timeouts / connection errors count as status 0
        status = 0
    stats.add(status, time.perf_counter() - start)
    return status, payload


def discover_subscribers(base: str, stats: Stats, timeout: float) -> list[str]:
    for _ in range(60):
        status, payload = call(base, "GET", "/v1/subscribers?limit=100", None, stats, timeout)
        if status == 200 and payload:
            return [s["id"] for s in payload]
        time.sleep(2)
    raise SystemExit("could not list subscribers - is the API up?")


def burst(base: str, stats: Stats, timeout: float) -> None:
    """New subscriber on the basic plan: cross 80%, then 100% of the data allowance."""
    msisdn = "+1555" + "".join(random.choice("0123456789") for _ in range(7))
    status, sub = call(
        base, "POST", "/v1/subscribers",
        {"msisdn": msisdn, "name": "Loadgen Subscriber", "plan_id": "basic"}, stats, timeout,
    )
    if status != 201 or not sub:
        return
    call(base, "POST", "/v1/usage",
         {"subscriber_id": sub["id"], "kind": "data", "amount": int(BASIC_DATA_QUOTA_MB * 0.85)}, stats, timeout)
    call(base, "POST", "/v1/usage",
         {"subscriber_id": sub["id"], "kind": "data", "amount": int(BASIC_DATA_QUOTA_MB * 0.2)}, stats, timeout)


def top_up(base: str, ids: list[str], stats: Stats, timeout: float) -> None:
    """A customer tops up; 1 in 10 taps "pay" twice, which must not charge twice (same Idempotency-Key)."""
    method = random.choices([m for m, _ in PAYMENT_METHODS], weights=[w for _, w in PAYMENT_METHODS])[0]
    body = {"subscriber_id": random.choice(ids), "amount_cents": random.choice(TOPUP_AMOUNTS_CENTS), "payment_method": method}
    headers = {"Idempotency-Key": f"app-{uuid.uuid4().hex}"}
    call(base, "POST", "/v1/topups", body, stats, timeout, headers)
    if random.random() < 0.10:
        call(base, "POST", "/v1/topups", body, stats, timeout, headers)


def one_request(base: str, ids: list[str], stats: Stats, timeout: float) -> None:
    roll = random.random()
    sub_id = random.choice(ids)
    if roll < 0.35:      # open the app: plan usage, balance and active bundles
        call(base, "GET", f"/v1/subscribers/{sub_id}", None, stats, timeout)
    elif roll < 0.45:    # balance widget
        call(base, "GET", f"/v1/subscribers/{sub_id}/balance", None, stats, timeout)
    elif roll < 0.50:    # browse offers
        call(base, "GET", random.choice(["/v1/plans", "/v1/bundles"]), None, stats, timeout)
    elif roll < 0.53:    # top-up history
        call(base, "GET", f"/v1/subscribers/{sub_id}/topups", None, stats, timeout)
    elif roll < 0.75:    # usage records coming from the network
        kind = random.choice(["data", "voice", "sms"])
        amount = {"data": random.randint(1, 20), "voice": random.randint(1, 3), "sms": 1}[kind]
        call(base, "POST", "/v1/usage", {"subscriber_id": sub_id, "kind": kind, "amount": amount}, stats, timeout)
    elif roll < 0.87:
        top_up(base, ids, stats, timeout)
    elif roll < 0.95:    # buy a bundle (402 when the balance is too low: a normal outcome)
        call(base, "POST", f"/v1/subscribers/{sub_id}/bundles", {"bundle_id": random.choice(BUNDLE_IDS)}, stats, timeout)
    else:
        burst(base, stats, timeout)


def worker(base: str, ids: list[str], stats: Stats, interval: float, deadline: float, timeout: float) -> None:
    next_at = time.monotonic()
    while time.monotonic() < deadline:
        one_request(base, ids, stats, timeout)
        next_at += interval
        delay = next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_at = time.monotonic()  # fell behind (slow responses): don't try to catch up


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--rps", type=float, default=5.0, help="target requests per second")
    parser.add_argument("--workers", type=int, default=0, help="threads (default: auto)")
    parser.add_argument("--duration", type=float, default=0, help="seconds to run (0 = until interrupted)")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-request timeout in seconds")
    parser.add_argument("--report-every", type=float, default=10.0)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    stats = Stats()
    ids = discover_subscribers(base, stats, args.timeout)
    stats.drain()

    workers = args.workers or max(1, min(32, int(args.rps)))
    interval = workers / args.rps
    deadline = time.monotonic() + args.duration if args.duration else float("inf")
    print(f"loadgen: {args.rps} rps over {workers} workers against {base} ({len(ids)} subscribers)", flush=True)

    threads = [
        threading.Thread(target=worker, args=(base, ids, stats, interval, deadline, args.timeout), daemon=True)
        for _ in range(workers)
    ]
    for t in threads:
        t.start()

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(args.report_every)
            lat, statuses = stats.drain()
            if not lat:
                continue
            errors = sum(n for s, n in statuses.items() if s == 0 or s >= 500)
            print(
                f"[loadgen] req={len(lat)} err={errors} "
                f"p50={percentile(lat, .5) * 1000:.0f}ms p95={percentile(lat, .95) * 1000:.0f}ms "
                f"statuses={dict(statuses)}",
                flush=True,
            )
    except KeyboardInterrupt:
        print("loadgen: stopped", flush=True)


if __name__ == "__main__":
    main()
