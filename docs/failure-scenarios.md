# Failure scenarios and recovery runbook

Three required scenarios (pod failure, elevated latency, stuck queue), a payment-provider incident for the
self-care top-up flow, plus two bonus ones.
Each one follows the same shape: **inject → detect → isolate the layer → recover → capture evidence.**

For the final demo, pick one scenario and tell it as a story: *"An alert fired. Here is how I found
which layer was broken in under N minutes."* Record the timestamps below; they are your MTTD / MTTR numbers.

| Time | Event |
|------|-------|
| T0 | Failure injected |
| T1 | First alert fired (time to detect = T1 - T0) |
| T2 | Layer identified on the triage dashboard (time to diagnose = T2 - T1) |
| T3 | Service healthy again (time to recover = T3 - T0) |

## How to reach the control endpoints

Chaos endpoints exist only when `CHAOS_ENABLED=true` (docker-compose and `values-dev.yaml`; **off** in prod)
and are never routed by the Ingress.

**docker-compose:** `usage-api` is on `localhost:8080`, `notification-service` on `localhost:8081`.

**EKS:**

```bash
kubectl -n telecom-dev port-forward svc/usage-api 8080:8080 &
kubectl -n telecom-dev port-forward svc/notification-service 8081:8081 &
```

On Windows PowerShell use `Invoke-RestMethod` (the `curl` alias is something else, and `curl.exe` mangles JSON quotes):

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8080/chaos/latency -ContentType application/json -Body '{"delay_ms":800}'
```

Bash equivalent: `curl -XPOST localhost:8080/chaos/latency -H 'content-type: application/json' -d '{"delay_ms":800}'`

Keep traffic flowing during every scenario (`make loadgen`, or `loadgen.enabled=true` in the chart),
otherwise there is nothing to measure.

---

## Scenario 1 - Pod failure

**Injected failure:** a usage-api pod dies mid-traffic.

**Inject**

```bash
# EKS: kill one pod (replicas >= 2 so the service survives)
kubectl -n telecom-dev delete "$(kubectl -n telecom-dev get pod -l app.kubernetes.io/name=usage-api -o name | head -1)"
# or kill the process from the inside (the container restarts):
curl -XPOST localhost:8080/chaos/crash
# docker-compose:
docker compose kill usage-api
```

**What you should see**

- `kube_pod_container_status_restarts_total` / pod `Terminating` -> `ContainerCreating` -> `Running`
- A short blip in `http_requests_total{status=~"5.."}` or connection errors in the load generator,
  limited to requests in flight on the dead pod (the ALB health check and `/readyz` remove it quickly).
- With `replicas >= 2` and the PodDisruptionBudget, availability stays above the SLO.

**Isolate the layer**

1. Pod layer: `kubectl get pods`, `kubectl describe pod` (Events, `Last State`, exit code).
2. App layer: logs of the previous container: `kubectl logs <pod> --previous` (look for `chaos: crashing process`).
3. Dependencies healthy? `/readyz` on the new pod shows `database: ok`.

**Recover:** automatic (Deployment recreates the pod). Verify `kubectl rollout status deploy/usage-api`.

**Evidence:** pod restart panel, request rate / error ratio during the blip, time the new pod became Ready.

**Lesson to state:** probes + multiple replicas + PDB turn a pod failure into a non-event; the SLO burn is tiny.

---

## Scenario 2 - Elevated latency

**Injected failure:** every API request is slowed by 800 ms (a stand-in for a slow dependency).

**Inject**

```bash
curl -XPOST localhost:8080/chaos/latency -H 'content-type: application/json' \
     -d '{"delay_ms": 800, "probability": 1.0}'
```

**What you should see**

- `histogram_quantile(0.95, ...http_request_duration_seconds_bucket...)` jumps above the 300 ms objective.
- The latency SLI (share of requests under 0.3 s) collapses; the **latency burn-rate alert** fires.
- `http_requests_in_flight` rises (requests pile up); CPU stays low - this is not a capacity problem.
- `db_query_duration_seconds` stays flat: **the database is fine**.
- Errors stay at ~0: availability SLI is unaffected. Only latency is burning.

**Isolate the layer** (this is the point of the triage dashboard)

| Check | Result | Conclusion |
|---|---|---|
| ALB target response time vs. app latency | same | not the network / load balancer |
| Pod CPU/memory, throttling | normal | not Kubernetes resources |
| `db_query_duration_seconds`, RDS CPU/connections | normal | not the database |
| `sqs_publish_duration_seconds` | normal | not the queue |
| App request duration, `chaos_injection_active{type="latency"}` | 1 | slowness is inside the app |

**Recover**

```bash
curl -XPOST localhost:8080/chaos/reset
```

Verify the p95 returns under 300 ms and the alert resolves.

**Evidence:** p95 panel before / during / after, alert timeline, the triage table above filled in.

Variant: `{"delay_ms": 1500, "probability": 0.2}` simulates intermittent slowness (only some requests).

---

## Scenario 3 - Stuck queue processing

**Injected failure:** the Notification consumer stops consuming, but the pod looks perfectly healthy.

**Inject**

```bash
curl -XPOST localhost:8081/chaos/stall -H 'content-type: application/json' -d '{"enabled": true}'
```

**What you should see**

- Kubernetes shows everything green: pod `Running`, `Ready`, no restarts, `/healthz` and `/readyz` return 200.
  (This is deliberate: it models a wedged consumer that pod-level checks cannot catch.)
- The API keeps returning 201s - **availability and latency SLOs are fine**. The damage is invisible to them.
- SQS (CloudWatch `AWS/SQS`): `ApproximateNumberOfMessagesVisible` and **`ApproximateAgeOfOldestMessage` climb**.
- `notifications_processed_total{result="sent"}` flatlines; `notification_worker_stalled` = 1.
- The **queue-age alert** fires (e.g. oldest message > 120 s) - this is the signal that catches it.

Locally you can read the queue directly:

```bash
docker compose exec localstack awslocal sqs get-queue-attributes \
  --queue-url http://localhost:4566/000000000000/usage-alerts-dev \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
```

**Isolate the layer**

1. API healthy? Yes (success rate, latency normal, `sqs_publish_total{result="ok"}` increasing) -> producer side is fine.
2. Queue growing? Yes -> messages are arriving but not leaving.
3. Consumer pods healthy? Yes -> not a crash.
4. Consumer throughput = 0 (`rate(notifications_processed_total[5m])`), `notification_worker_stalled` = 1 -> the consumer is wedged.

**Recover**

```bash
curl -XPOST localhost:8081/chaos/stall -H 'content-type: application/json' -d '{"enabled": false}'
```

The backlog drains; `ApproximateAgeOfOldestMessage` falls back to ~0 and the alert resolves.
In a real incident, the equivalent action is restarting the consumer pods (`kubectl rollout restart deploy/notification-service`).

**Evidence:** queue depth and age curves, the "pod healthy but queue growing" contrast, drain time after recovery.

**Lesson to state:** pod and API health do not prove the pipeline works - alert on **queue age**, not just pod status.

---

## Scenario 4 - Payment provider degraded

**Injected failure:** the external payment provider behind top-ups becomes slow, then starts failing.
Everything we run is healthy; customers simply cannot add credit.

**Inject** (on `usage-api`, port 8080)

```bash
# slow: 2.5 s per payment, above our 2 s timeout -> top-ups answer 504
curl -XPOST localhost:8080/chaos/payments -H 'content-type: application/json' -d '{"latency_ms": 2500}'
# or failing: half of the payments get a provider error -> 502
curl -XPOST localhost:8080/chaos/payments -H 'content-type: application/json' -d '{"error_rate": 0.5}'
# or an issuer outage: most cards declined -> 402 (customer-side, not our SLO)
curl -XPOST localhost:8080/chaos/payments -H 'content-type: application/json' -d '{"decline_rate": 0.6}'
```

**What you should see**

- `TopUpFailuresHigh` (critical, payments-team) after ~2 minutes; `PaymentProviderSlow` (warning) for the latency case.
- *SLO Overview*, row *Self-care: top-ups and payments*: top-up success drops, "failed" appears in *Top-ups by outcome*,
  *Money in per hour* falls: the business impact in one picture.
- The API availability burn rate rises too (502/504 are server errors), but only on the `/v1/topups` route.
- The decline case raises `TopUpDeclinesUnusual` instead and does **not** burn the availability SLO: a declined card is
  the customer's bank saying no, not our outage.

**Isolate the layer** (*Service Triage*, row 5)

| Check | Result | Conclusion |
|---|---|---|
| p95 of `POST /v1/topups` vs every other route | only top-ups are slow | not the nodes, not the API as a whole |
| DB p95 query time, queue publish latency | flat | not the database, not SQS |
| Payment provider p95 / results | p95 at the 2 s timeout, or `error` / `timeout` results | **the external payment provider** |

**Recover**

```bash
curl -XPOST localhost:8080/chaos/reset
```

In a real incident you cannot fix the provider: you contact them, show a "top-ups temporarily unavailable" banner,
and, if the provider supports it, fail over to a second payment route. Nobody was charged for failed top-ups
(no wallet credit, no money taken), and customers who retried with the same `Idempotency-Key` were not charged twice.

**Lesson to state:** an external dependency needs its own latency/error metrics and its own SLO; otherwise the
incident looks like "the API is slow" and the wrong team gets paged.

---

## Bonus A - Poison message and the dead-letter queue

```bash
curl -XPOST 'localhost:8080/chaos/poison-message?kind=malformed'
```

The consumer rejects it (`notifications_processed_total{result="invalid"}`), SQS redelivers it, and after
`maxReceiveCount` (3) it lands in the DLQ (~30 s locally, ~90 s with the 30 s visibility timeout on AWS).
Alert on **DLQ depth > 0**. Recover: inspect the message in the DLQ, fix or discard it, then redrive.

## Bonus B - Provider failures

```bash
curl -XPOST localhost:8081/chaos/failures -H 'content-type: application/json' -d '{"rate": 0.5}'
```

Half of deliveries fail and are retried (`notifications_processed_total{result="delivery_failed"}`); persistent failures end up in the DLQ.
Clear with `/chaos/reset`.

## After every scenario

Clear all injections (`POST /chaos/reset` on both services), confirm the SLIs are back in budget, and write
down what you would automate or alert on differently next time (this feeds the "lessons learned" minute of the presentation).
