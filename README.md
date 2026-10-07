# Telecom Self-Care & Top-Up Platform

The application layer for the **Week 4 project: Telecom Observability & SRE Platform for EKS**.

It is a small, deliberately realistic event-driven microservice app. Its job is to give the observability
platform something with real failure modes to observe: an HTTP API, a database, a queue and an asynchronous
consumer. When something breaks you must be able to say *which layer* broke.

```
 clients / loadgen
        |  HTTPS
        v
  +-----------+  usage record   +--------------+
  | ALB       |---------------->|  usage-api   |--- SQL ---> PostgreSQL (RDS / Aurora)
  +-----------+                 |  (FastAPI)   |                ^
                                +------+-------+                |
                                       | event: usage.threshold_crossed
                                       v                        |
                                 +-----------+   poll/delete    |
                                 | SQS queue |<-----------------+----+
                                 |   + DLQ   |                       |
                                 +-----------+            +----------+------------+
                                                          | notification-service |
                                                          | (SQS consumer)       |--> simulated SMS provider
                                                          +----------------------+
```

**Story (a normal day for a mobile operator's self-care app):** customers open the app to check their usage and
prepaid balance, **top up** by card, mobile money or voucher, **buy add-on bundles** (1 GB, 100 minutes, ...) from their
balance, and get SMS notifications: "you have used 80 % of your data", "top-up successful, new balance $23.50",
"you bought 1 GB data". Top-ups go through an external **payment provider** and are **idempotent** (a double tap on
"Pay" charges once). Usage records arrive from the network all day.

| Service | Role | Port | Scaling signal |
|---|---|---|---|
| `usage-api` | Self-care REST API: plans, subscribers, usage, wallet balance, top-ups (via the payment provider), bundles. Owns PostgreSQL tables, publishes events to SQS | 8080 | CPU (HPA) |
| `notification-service` | Long-polling SQS consumer, idempotent, writes an audit log, "sends" the notification | 8081 (probes + metrics only) | queue depth/age (KEDA later) |

## Project deliverables map

Where each Week 4 deliverable (brief section 9) lives:

| Deliverable | Location |
|---|---|
| Architecture diagram | [docs/architecture.md](docs/architecture.md) (runtime, request flow, CI/CD and identity) |
| Working EKS / container deployment | `services/*/Dockerfile`, `charts/telecom-app/`, deployed by `.github/workflows/deploy.yml` |
| Terraform / IaC | [terraform/](terraform/README.md): VPC, EKS, ECR, SQS + DLQ, RDS, IRSA, GitHub OIDC, CloudWatch, security services |
| GitHub Actions workflow | [.github/workflows/ci-cd.yml](.github/workflows/ci-cd.yml), `deploy.yml`, `rollback.yml` |
| Helm deployment artifacts | [charts/telecom-app/](charts/telecom-app) (+ `observability/k8s/` for the monitoring add-ons) |
| Security scan / findings report | [docs/security.md](docs/security.md), Trivy + Checkov results in GitHub code scanning |
| CloudWatch and Prometheus/Grafana dashboards | `charts/telecom-app/files/dashboards/`, `terraform/modules/observability` |
| Cost / optimization analysis | [docs/cost-optimization.md](docs/cost-optimization.md) |
| Failure / recovery runbook | [docs/failure-scenarios.md](docs/failure-scenarios.md), [docs/runbooks/deploy-and-rollback.md](docs/runbooks/deploy-and-rollback.md) |
| README and final presentation | this file, [docs/presentation-outline.md](docs/presentation-outline.md) |

## Why this app fits the project

| Project requirement | Where it shows up |
|---|---|
| Containers & K8s (#9) | Multi-stage non-root images, Helm chart with startup/liveness/readiness probes, HPA, PDB, hardened security context, graceful shutdown |
| Observability (#10) | `/metrics` on both services, JSON logs with correlation IDs, optional OpenTelemetry traces, SLI-ready metrics (below) |
| Security ops (#11) | No secrets in the image or chart (DB URL from a Secret, AWS access via IRSA), read-only root filesystem, dropped capabilities, chaos endpoints off in prod and never routed by the Ingress, MSISDNs masked in logs |
| Cost / optimization (#12) | Right-sizeable requests/limits, HPA, log verbosity and retention knobs, a one-pod dev profile |
| Failure test (Day 5) | Built-in failure injection for pod crash, latency, stuck queue, poison message, provider failures - see [docs/failure-scenarios.md](docs/failure-scenarios.md) |

## Repository layout

```
services/
  usage-api/            FastAPI app, Dockerfile, tests, tools/loadgen.py
  notification-service/ SQS worker + probe/metrics server, Dockerfile, tests
charts/telecom-app/     Helm chart (both services, loadgen, ServiceMonitors, PrometheusRule, dashboards) + values-dev/prod
  files/rules, files/dashboards   alert rules and Grafana dashboards (used by compose and Helm)
scripts/                smoke-test.sh (post-deploy), create-queues.sh (quick AWS path), localstack-init.sh (local)
observability/          Prometheus + Grafana provisioning for the local stack
  k8s/                  values for kube-prometheus-stack (Alertmanager -> SNS) and Tempo on EKS
terraform/              AWS infrastructure as code (state bootstrap, modules, dev/prod settings, add-on scripts)
.github/workflows/      ci-cd (test, scan, build, push, deploy), deploy (reusable), rollback
docs/                   architecture, security report, cost analysis, failure scenarios, runbooks, presentation outline
docker-compose.yml, Makefile, ruff.toml, .checkov.yaml, .trivyignore
```

## Quick start (local, Docker Desktop)

```bash
docker compose up --build -d          # postgres + localstack(SQS) + usage-api + notification-service
docker compose ps                      # wait until everything is "healthy"

curl localhost:8080/v1/subscribers?limit=3        # PowerShell: Invoke-RestMethod localhost:8080/v1/subscribers?limit=3
curl localhost:8080/readyz
curl localhost:8081/readyz
```

Generate traffic and look at the dashboards:

```bash
docker compose --profile loadgen up --build -d loadgen           # ~5 requests/s, including threshold-crossing bursts
docker compose --profile observability up -d                     # Prometheus :9090, Grafana :3000 (anonymous admin)
docker compose logs -f usage-api notification-service            # JSON logs: follow one correlation_id across both
```

Trigger an alert by hand (the first demo subscriber is on the "basic" plan: 2048 MB):

```bash
SUB=$(curl -s "localhost:8080/v1/subscribers?limit=1" | python -c "import sys,json;print(json.load(sys.stdin)[0]['id'])")
curl -XPOST localhost:8080/v1/usage -H 'content-type: application/json' -H 'X-Correlation-ID: demo-1' \
     -d "{\"subscriber_id\":\"$SUB\",\"kind\":\"data\",\"amount\":1700}"
docker compose logs notification-service | grep demo-1          # same correlation ID, other service
```

PowerShell version of the last two steps:

```powershell
$sub = (Invoke-RestMethod "http://localhost:8080/v1/subscribers?limit=1")[0].id
Invoke-RestMethod -Method Post -Uri http://localhost:8080/v1/usage -ContentType application/json `
  -Headers @{ "X-Correlation-ID" = "demo-1" } `
  -Body (@{ subscriber_id = $sub; kind = "data"; amount = 1700 } | ConvertTo-Json)
docker compose logs notification-service | Select-String demo-1
```

Re-running the same usage will not alert again (each threshold fires once per allowance cycle). Start a new cycle with
`POST /demo/reset-usage` (chaos/demo tools are enabled in the local stack), or let the load generator create fresh subscribers.

Stop everything: `docker compose --profile loadgen --profile observability down -v`.

## API summary (usage-api)

| Method & path | Purpose |
|---|---|
| `GET /healthz` | Liveness - process is up; does **not** touch the database |
| `GET /readyz` | Readiness - database reachable (critical). Queue state is reported (`ok` / `degraded`) but never fails readiness |
| `GET /metrics` | Prometheus metrics |
| `GET /v1/plans`, `GET /v1/bundles` | Plans and the add-on bundle catalogue |
| `GET /v1/subscribers?msisdn=` , `POST /v1/subscribers` | Find an account by phone number, create a subscriber (starts with a 0 balance) |
| `GET /v1/subscribers/{id}` | **Self-care home screen**: usage per kind (plan + active bundles), balance, active bundles |
| `GET /v1/subscribers/{id}/balance` | Prepaid balance |
| `POST /v1/topups` + header `Idempotency-Key` | Top up `{subscriber_id, amount_cents (100-20000), payment_method: card\|mobile_money\|voucher}`. 201 credited, 402 declined by the bank, 502/504 payment provider error/timeout, **200 + `Idempotent-Replayed: true`** when the same key is sent again (nothing charged twice) |
| `GET /v1/topups/{id}`, `GET /v1/subscribers/{id}/topups` | Top-up status and history |
| `POST /v1/subscribers/{id}/bundles` | Buy a bundle `{bundle_id}` from the balance: 201, or 402 when the balance is too low |
| `POST /v1/usage` | Record usage `{subscriber_id, kind: data\|voice\|sms, amount}` (from the network); returns which alerts were published |
| `POST /chaos/payments` | Degrade the payment provider `{latency_ms, error_rate, decline_rate}` (scenario 4) |
| `/chaos/*`, `/demo/*` | Failure injection and demo helpers - only when `CHAOS_ENABLED=true` |

Interactive docs: <http://localhost:8080/docs>.

## SLIs and the metrics behind them

Suggested objectives (adjust as you like - they are *your* SLOs to define on Day 1):

| SLI | Objective | Source |
|---|---|---|
| API availability | 99.9 % non-5xx | `http_requests_total` |
| API latency | 95 % of requests < 300 ms | `http_request_duration_seconds` (a `le="0.3"` bucket exists on purpose) |
| Notification freshness | 95 % delivered < 60 s after the event | `notification_queue_delay_seconds` and CloudWatch `ApproximateAgeOfOldestMessage` |
| Poison / failed messages | DLQ depth = 0 | CloudWatch `ApproximateNumberOfMessagesVisible` on the DLQ |

```promql
# Availability (exclude probe/scrape routes)
sum(rate(http_requests_total{service="usage-api",status!~"5..",route!~"/healthz|/readyz|/metrics"}[5m]))
/ sum(rate(http_requests_total{service="usage-api",route!~"/healthz|/readyz|/metrics"}[5m]))

# Latency SLI: share of requests faster than 300 ms
sum(rate(http_request_duration_seconds_bucket{service="usage-api",le="0.3",route!~"/healthz|/readyz|/metrics"}[5m]))
/ sum(rate(http_request_duration_seconds_count{service="usage-api",route!~"/healthz|/readyz|/metrics"}[5m]))

# p95 latency
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{service="usage-api",route!~"/healthz|/readyz|/metrics"}[5m])))

# Notification end-to-end delay (p95)
histogram_quantile(0.95, sum by (le) (rate(notification_queue_delay_seconds_bucket[5m])))

# Consumer throughput and failures (a flat line at 0 while the queue grows = stuck consumer)
sum(rate(notifications_processed_total{result="sent"}[5m]))
sum by (result) (rate(notifications_processed_total{result!~"sent|duplicate"}[5m]))

# Database
histogram_quantile(0.95, sum by (le, operation) (rate(db_query_duration_seconds_bucket[5m])))
db_pool_connections_in_use
```

Full metric list:

| Metric | Service | Meaning |
|---|---|---|
| `http_requests_total{method,route,status}` | api | Requests; `route` is the template (e.g. `/v1/subscribers/{subscriber_id}`), so cardinality stays bounded |
| `http_request_duration_seconds{method,route}` | api | Latency histogram |
| `http_requests_in_flight` | api | Concurrent requests |
| `usage_records_total{kind}`, `usage_alert_events_total{kind,threshold}` | api | Business throughput |
| `sqs_publish_total{result}`, `sqs_publish_duration_seconds` | api | Queue producer health |
| `db_query_duration_seconds{operation}`, `db_pool_connections_in_use` | both | Database layer |
| `sqs_messages_received_total`, `sqs_receive_errors_total`, `sqs_delete_errors_total` | notification | Queue consumer health |
| `notifications_processed_total{result}` | notification | `sent`, `duplicate`, `invalid`, `delivery_failed`, `error` |
| `notification_processing_seconds`, `notification_queue_delay_seconds` | notification | Processing time and event-to-delivery delay |
| `notification_worker_last_poll_timestamp_seconds`, `notification_worker_stalled` | notification | Consumer loop heartbeat / injected stall |
| `chaos_injection_active{type}` | both | 1 while a failure injection is active (annotate your dashboards with it) |

SQS queue depth, age and DLQ depth come from **CloudWatch** (`AWS/SQS`), RDS metrics from `AWS/RDS`; use the Grafana
CloudWatch data source for those panels.

## Dashboards and alerts

Everything lives in `charts/telecom-app/files/` (single source of truth for both docker-compose and EKS):

| File | What it is |
|---|---|
| `rules/telecom.rules.yaml` | Recording rules (error ratio and slow-request ratio over 5m / 30m / 1h / 6h) and all alerts |
| `dashboards/slo-overview.json` | **Start here.** Objectives right now, availability and latency burn rates, alert table and history, produced-vs-consumed events |
| `dashboards/service-triage.json` | **Where is it broken?** One tile per layer at the top, then rows for Edge/API, Database, Queue producer, Consumer, and failure-injection markers |

Start it locally and open <http://localhost:3000> (the SLO Overview is the home dashboard; Prometheus is on <http://localhost:9090>, with the alerts at `/alerts`):

```bash
docker compose --profile observability up -d
docker compose --profile loadgen up --build -d loadgen     # something to look at
```

Purple markers on every graph show when a failure injection was active, so you can line up cause and effect.

**Alerts** (every alert has a severity, an owner and a runbook link):

| Alert | Severity | Owner | Fires when |
|---|---|---|---|
| `ApiAvailabilityFastBurn` | critical | platform-sre | 5xx ratio burns the 99.9% budget > 14.4x over 1h **and** 5m |
| `ApiAvailabilitySlowBurn` | warning | platform-sre | > 6x over 6h **and** 30m |
| `ApiLatencyFastBurn` | critical | platform-sre | > 30% of requests slower than 300 ms over 1h **and** 5m |
| `ApiLatencySlowBurn` | warning | platform-sre | > 10% slower than 300 ms over 6h **and** 30m |
| `NotificationConsumerStalled` | critical | notifications-team | the API produces alert events but the consumer receives none for 2m (pods can still look healthy) |
| `NotificationWorkerHeartbeatLost` | critical | notifications-team | the consumer loop stopped iterating |
| `NotificationDeliveryFailures` | warning | notifications-team | failed / invalid messages are heading to the dead-letter queue |
| `NotificationDelayHigh` | warning | notifications-team | p95 event-to-notification delay above 60 s |
| `SqsPublishErrors` | critical | platform-sre | the API cannot publish to SQS |
| `TargetDown` | critical | platform-sre | a service is not being scraped |
| `DatabaseQueriesSlow` | warning | data-platform | p95 query time above 250 ms |
| `TopUpFailuresHigh` | critical | payments-team | more than 5% of top-ups fail on our side (provider errors/timeouts; declines excluded) |
| `PaymentProviderSlow` | warning | payments-team | payment provider p95 above 1 s |
| `TopUpDeclinesUnusual` | warning | payments-team | more than 25% of top-ups declined by banks/wallets for 10 min |
| `ChaosInjectionActive` | info | platform-sre | a deliberate failure test is on (explains the other alerts) |

The burn-rate alerts use the multi-window, multi-burn-rate method: a long window proves the budget is really being
spent, a short window proves it is still burning now. The 3-day and 1-day "ticket" windows from the Google SRE
workbook are left out because a demo does not run that long; add them for a real service.

On EKS, Alertmanager routes on those labels (`observability/k8s/kube-prometheus-stack.yaml`): `critical` and `warning`
go to the same SNS topic as the CloudWatch alarms (e-mail subscription), critical repeats hourly, a critical alert
inhibits the warning with the same name, and `info` stays on the dashboards. The EKS control-plane targets that cannot
be scraped are switched off so the default rules do not page on them.

On EKS, set `monitoring.prometheusRule.enabled=true` and `monitoring.grafanaDashboards.enabled=true` (both on in
`values-dev.yaml` and `values-prod.yaml`). The chart then creates a `PrometheusRule` and one ConfigMap per dashboard
labelled `grafana_dashboard: "1"`, which kube-prometheus-stack's Grafana sidecar picks up. The `monitoring.serviceMonitor.labels`
value (default `release: kube-prometheus-stack`) must match your Prometheus' rule/monitor selectors. Dashboards assume a
Prometheus data source with uid `prometheus` (the kube-prometheus-stack default). Queue depth/age, RDS and node panels are on
the CloudWatch dashboard `telecom-<env>-aws-services` that Terraform creates, next to the matching CloudWatch alarms.

## Correlation across services

Every request gets a correlation ID (`X-Correlation-ID` header, generated if absent). It is:

1. stored in a context variable and added to **every JSON log line** (`correlation_id`),
2. echoed back in the response header,
3. stored with the usage record,
4. put in the SQS message **attributes** and the event body,
5. restored by the Notification service, so its logs carry the same ID.

On EKS the container logs reach CloudWatch through Container Insights, which nests each JSON line under `log_processed`.
Terraform saves ready-made Logs Insights queries (`telecom-<env>/01-follow-a-correlation-id`, errors by service, slow
requests, notification outcomes, failure-injection audit). By hand:

```
fields @timestamp, log_processed.service, log_processed.message
| filter log_processed.correlation_id = "demo-1"
| sort @timestamp asc
```

Optional tracing: set `OTEL_ENABLED=true` and `OTEL_EXPORTER_OTLP_ENDPOINT` (Helm: `otel.endpoint`,
`*.config.otelEnabled`). FastAPI, SQLAlchemy and boto3 calls are instrumented, the W3C `traceparent` travels in the SQS
attributes, and the consumer continues the producer's trace. `trace_id` is added to the logs when tracing is on.
In dev on EKS tracing is on by default and goes to Tempo (installed by `terraform/scripts/install-addons.sh`), visible in
Grafana's *Explore > Tempo*.

## Configuration (environment variables)

| Variable | Default | Used by |
|---|---|---|
| `DATABASE_URL` | local SQLite file | both - e.g. `postgresql+psycopg://user:pw@host:5432/db` |
| `SQS_QUEUE_URL` | empty (events dropped / worker disabled) | both |
| `SQS_ENDPOINT_URL` | empty | both - only for LocalStack |
| `AWS_REGION` | `us-east-1` | both |
| `ALERT_THRESHOLDS` | `80,100` | api |
| `AUTO_MIGRATE`, `SEED_SUBSCRIBERS` | `true`, `25` | api - creates tables and demo data at startup |
| `SQS_WAIT_SECONDS`, `WORKER_STALE_SECONDS` | `10`, `60` | notification |
| `CHAOS_ENABLED` | `false` | both |
| `LOG_LEVEL`, `ENVIRONMENT`, `SERVICE_NAME` | `INFO`, `dev` | both |
| `OTEL_ENABLED` | `false` | both |

## Deploying to AWS (EKS)

The full path is code, end to end:

1. **Infrastructure:** `terraform/` builds the VPC, EKS, ECR, SQS + DLQ, RDS, IAM roles (IRSA and GitHub OIDC), CloudWatch
   alarms, dashboard and saved queries, and the security services. It also publishes the Helm wiring for each environment
   (registry, queue URL, role ARNs, ALB subnets) to SSM. See [terraform/README.md](terraform/README.md).
2. **Cluster add-ons:** `terraform/scripts/install-addons.sh dev` installs metrics-server, the AWS Load Balancer Controller,
   kube-prometheus-stack (with Alertmanager -> SNS) and Tempo; `terraform/scripts/sync-db-secret.sh dev` creates the
   namespace and the database Secret from Secrets Manager.
3. **Application:** push to `main`. GitHub Actions tests, scans and builds the images (tag = commit SHA), pushes them to ECR
   with OIDC credentials and deploys with `helm upgrade --atomic`, then runs `scripts/smoke-test.sh`. Setup and rollback:
   [docs/runbooks/deploy-and-rollback.md](docs/runbooks/deploy-and-rollback.md).

Dev and prod are separate namespaces with separate values files (`values-dev.yaml`: failure injection, load generator and
tracing on; `values-prod.yaml`: failure injection and load generator off, `seedSubscribers: 0`), separate queues and databases.
Both enable the NetworkPolicies (default deny; the API accepts traffic only from the ALB subnets, its own namespace and Prometheus).

Manual deploy without CI (for example the very first time):

```bash
terraform -chdir=terraform output -raw helm_values > values-aws.generated.yaml
helm upgrade --install telecom charts/telecom-app -n telecom-dev \
  -f charts/telecom-app/values-dev.yaml -f values-aws.generated.yaml \
  --set usageApi.image.tag=$TAG --set notificationService.image.tag=$TAG --atomic --wait
./scripts/smoke-test.sh telecom-dev
```

## Tests

```bash
# Fast unit tests: threshold logic, message handling, failure injection (no AWS, no database).
# Needs only:  pip install prometheus-client starlette
make test-unit

# Full suites: API with real FastAPI + mocked SQS (moto), worker against mocked SQS
cd services/usage-api            && python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt && pytest
cd services/notification-service && python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt && pytest
```

On Windows PowerShell activate with `.venv\Scripts\Activate.ps1`.

## Design decisions and known limitations

- **Readiness vs. liveness.** Liveness never touches dependencies (a database outage must not restart pods). Readiness
  checks the database only; a degraded queue is *reported* but does not pull the API out of the load balancer.
- **Stuck consumer is invisible to pod checks - on purpose.** The injected stall keeps `/healthz` and `/readyz` green so
  you can demonstrate why queue-age alerts matter. The (real) liveness check does fail if the worker loop dies or stops iterating.
- **At-least-once delivery + idempotency.** SQS may redeliver; the unique `event_id` makes processing idempotent. Messages are
  deleted only after success; failures are retried and end up in the DLQ after 3 receives.
- **Dual write.** The usage row is committed, then the event is published. If the publish fails, usage is kept and the failure
  shows in `alerts_failed` and `sqs_publish_total{result="error"}`. The production fix is a transactional outbox.
- **Schema management.** Tables are created at startup (`AUTO_MIGRATE`) with retries - fine for a demo. Use Alembic migrations
  in a pre-deploy Job for real systems. Both services share one database instance, with separate tables.
- **One uvicorn worker per container.** Kubernetes scales by pods; it keeps Prometheus metrics exact.
- **Notification concurrency.** One consumer thread per pod; scale by replicas. CPU is a poor autoscaling signal for a queue
  worker - KEDA on `ApproximateNumberOfMessagesVisible` is the better next step (HPA is off for it by default).
- **Dependencies use lower bounds.** Each image is reproducible by its SHA tag, but a lock file (e.g. `pip-compile`) would make
  rebuilds reproducible too.
- **Next steps, not included:** External Secrets Operator (the DB secret is synced by script), KEDA for the consumer, WAF and
  HTTPS on the ALB, egress NetworkPolicies, transactional outbox, Alembic migrations. See also `docs/security.md` section 5.

## Verification status

**Verified by running it:** the local stack (`docker compose`, both profiles) on Docker Desktop: readiness of both services,
a usage record crossing 80 % and 100 % producing two events that were consumed and "sent"; Prometheus loaded all 20 rules
with no evaluation errors and every dashboard query returned data; Grafana provisioned both dashboards; the stuck-queue
scenario fired `NotificationConsumerStalled` about 3 minutes after injection while both probes stayed at 200, and cleared
after recovery. The pure-logic unit tests pass.

**Checked statically only** (the environment that wrote them could not download Terraform, Helm or Python packages):
the Helm chart renders in every value combination, including the Terraform-shaped values, with strict YAML (no duplicate
keys) and the expected objects; every Terraform reference, module input and variable resolves and the formatting follows
`terraform fmt` alignment rules; the workflows are valid YAML; shell scripts pass `bash -n`.

**First real run happens in CI:** the pytest integration suites (FastAPI + moto), `ruff`, `helm lint`, kubeconform,
`terraform validate`, Trivy and Checkov all run in `ci-cd.yml` on the first push. Expect to fix a few findings there; the
`terraform apply` itself is the first test of the AWS side.
