# Architecture

Telecom usage-alerts platform on Amazon EKS, built so that an outage can be traced to **one layer**:
load balancer, Kubernetes, application code, queue, database or network. Diagrams are Mermaid (GitHub renders them).

## 1. Runtime (AWS, one environment)

```mermaid
flowchart LR
  client([Clients / loadgen]) -->|HTTP /v1| alb[ALB<br/>AWS Load Balancer Controller]

  subgraph vpc[VPC 10.20.0.0/16 - 2-3 AZs]
    subgraph pub[Public subnets]
      alb
      nat[NAT gateway]
    end
    subgraph priv[Private subnets - EKS managed node group]
      subgraph ns[namespace telecom-env - NetworkPolicy default deny]
        api[usage-api<br/>FastAPI, HPA 2-6, PDB]
        notif[notification-service<br/>SQS consumer, PDB]
      end
      subgraph mon[namespace monitoring]
        prom[Prometheus + Alertmanager]
        graf[Grafana]
        tempo[Tempo]
      end
      fb[Fluent Bit + CloudWatch agent<br/>Container Insights add-on]
      rds[(RDS PostgreSQL 16<br/>KMS, TLS only)]
    end
    s3ep[S3 gateway endpoint]
  end

  alb --> api
  api -->|SQL| rds
  api -->|usage.threshold_crossed| sqs[[SQS usage-alerts<br/>KMS, TLS only]]
  sqs -->|long poll| notif
  sqs -. 3 failed receives .-> dlq[[SQS DLQ]]
  notif -->|audit row| rds
  notif -->|simulated SMS| sms([SMS provider])

  prom -->|scrape /metrics| api & notif
  api & notif -->|OTLP traces| tempo
  graf --> prom & tempo
  fb -->|container logs| cwl[(CloudWatch Logs)]
  prom -->|alerts| am[Alertmanager] -->|sigv4| sns[[SNS alarms topic]] --> email([On-call email])
  cwa[CloudWatch alarms<br/>SQS age, DLQ, RDS, nodes] --> sns
```

| Layer | Component | Failure signal (where you see it) |
|---|---|---|
| Edge | ALB (Ingress, only `/v1` routed) | ALB 5xx / target health (CloudWatch), API 5xx ratio (Grafana) |
| Kubernetes | EKS, HPA, PDB, probes, NetworkPolicy | `up`, pod restarts, failed nodes alarm, Container Insights |
| Application | usage-api, notification-service | SLO burn-rate alerts, p95 latency, error ratio, logs by correlation ID |
| Queue | SQS + DLQ | queue age / backlog / DLQ alarms, produced-vs-consumed panel, `NotificationConsumerStalled` |
| Database | RDS PostgreSQL | DB p95 query time (app metric), RDS CPU / connections / storage alarms, slow-query log |
| Network | VPC, NAT, endpoints | VPC flow logs, ALB vs app latency comparison on the triage dashboard |

## 2. One request, end to end (correlation ID and trace)

```mermaid
sequenceDiagram
  autonumber
  participant C as Client
  participant A as usage-api
  participant D as RDS
  participant Q as SQS
  participant N as notification-service
  C->>A: POST /v1/usage (X-Correlation-ID optional)
  A->>A: middleware assigns correlation_id, starts span, times request
  A->>D: UPDATE usage_totals ... FOR UPDATE / INSERT usage_records
  A->>Q: SendMessage(body=event, attributes: correlation_id, traceparent)
  A-->>C: 201 + X-Correlation-ID
  N->>Q: ReceiveMessage (long poll 10 s)
  N->>N: restore correlation_id + trace context, dedupe on event_id
  N->>D: INSERT notifications (unique event_id = idempotent)
  N->>Q: DeleteMessage (only after success)
  Note over A,N: Same correlation_id in every log line of both services and in the stored rows,<br/>same trace in Tempo, queue delay measured from SentTimestamp.
```

## 3. Delivery (CI/CD) and identity

```mermaid
flowchart LR
  dev([Developer]) -->|PR| gh[GitHub]
  gh --> ci{{ci-cd.yml}}
  ci --> t[pytest + ruff] & h[helm lint + kubeconform] & tf[terraform fmt/validate] & sec[Trivy + Checkov]
  t & h & tf & sec --> b[docker build<br/>tag = commit SHA<br/>Trivy image gate]
  b -->|main only, OIDC role ecr-push| ecr[(ECR immutable tags)]
  ecr --> d[deploy.yml - GitHub Environment dev<br/>approval, OIDC role deploy]
  d -->|helm upgrade, atomic| eks[(EKS telecom-dev)]
  d --> smoke[smoke test] -->|fail| rb[helm rollback]
  ssm[(SSM /telecom/env/helm-values)] --> d
  terraform[Terraform] --> ssm & ecr & eks
```

No long-lived AWS keys exist anywhere: GitHub Actions exchanges its OIDC token for short-lived role credentials,
and each role trusts one claim only (`ref:refs/heads/main` to push images, `environment:<env>` to deploy).
Pods get AWS access through IRSA roles bound to one service account each.

## 4. Where things are defined

| Concern | Code |
|---|---|
| AWS infrastructure | `terraform/` (modules: network, eks, ecr, sqs, rds, irsa, github_oidc, observability, security, kms) |
| Kubernetes workloads | `charts/telecom-app/` (values-dev / values-prod; AWS wiring comes from Terraform via SSM) |
| Cluster add-ons | `terraform/scripts/install-addons.sh` + `observability/k8s/*.yaml` |
| Alert rules and dashboards | `charts/telecom-app/files/rules`, `charts/telecom-app/files/dashboards` (same files used locally) |
| CloudWatch alarms, dashboard, Logs Insights queries | `terraform/modules/observability` |
| Pipeline | `.github/workflows/` |
