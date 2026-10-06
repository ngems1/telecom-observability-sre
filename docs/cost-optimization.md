# Cost and optimization analysis

Brief topic #12 and section 8: baseline, right-sizing, autoscaling, lifecycle controls, and at least three practical
optimization actions **supported by evidence**. Fill the "measured" columns from your own account: the estimates below
are list prices (us-east-1, on-demand) at the time of writing and should be checked in the AWS Pricing Calculator.

## 1. Baseline (dev, always on)

| Item | Unit price (approx.) | Dev sizing | Per month |
|---|---|---|---|
| EKS control plane | $0.10 / hour | 1 cluster | $73 |
| EC2 nodes | t3.large $0.0832 / hour | 2 nodes | $121 |
| NAT gateway | $0.045 / hour + $0.045 / GB processed | 1 (shared) | $33 + data |
| Application Load Balancer | ~$0.0225 / hour + LCUs | 1 | $17+ |
| RDS PostgreSQL | db.t4g.micro ~$0.016 / hour + gp3 storage | 1, 20 GiB, single-AZ | $14 |
| EBS (nodes + Prometheus/Grafana/Tempo volumes) | gp3 $0.08 / GB-month | ~80 GiB | $6 |
| CloudWatch Logs | $0.50 / GB ingested, $0.03 / GB-month stored | container, control-plane, flow, trail logs | $5-20 |
| CloudWatch metrics / alarms / dashboard | per metric / alarm / dashboard | Container Insights + 7 alarms + 1 dashboard | $5-15 |
| KMS, Secrets Manager, SNS, SQS, ECR storage | small fixed + per request | 1 key, 1 secret | $3-5 |
| GuardDuty, Inspector, Security Hub | usage based (free trials first 30 days) | account | $5-20 |
| **Total** | | | **~ $280-320 / month (~ $10 / day)** |

**Measured baseline:** after one full day of running, record the real numbers:

```bash
# Cost by service for this project (needs the default tags Terraform applies: Project, Environment)
aws ce get-cost-and-usage --time-period Start=$(date -d '-7 days' +%F),End=$(date +%F) \
  --granularity DAILY --metrics UnblendedCost \
  --filter '{"Tags":{"Key":"Project","Values":["telecom"]}}' \
  --group-by Type=DIMENSION,Key=SERVICE
```

Activate `Project` and `Environment` as cost allocation tags once (Billing console > Cost allocation tags);
data appears after about 24 hours.

## 2. Right-sizing method (evidence from our own telemetry)

Requests decide how many nodes you pay for; actual usage decides what you need. Compare them in Prometheus:

```promql
# CPU actually used vs requested, per container (cores)
sum by (container) (rate(container_cpu_usage_seconds_total{namespace="telecom-dev", container!=""}[30m]))
sum by (container) (kube_pod_container_resource_requests{namespace="telecom-dev", resource="cpu"})

# Memory working set vs requested (bytes)
max by (container) (container_memory_working_set_bytes{namespace="telecom-dev", container!=""})
sum by (container) (kube_pod_container_resource_requests{namespace="telecom-dev", resource="memory"})

# Node utilization (how much of what we pay for is used)
1 - avg(rate(node_cpu_seconds_total{mode="idle"}[30m]))
```

Rule of thumb used here: request = p95 usage under normal load (loadgen at 5 rps) + 30 % headroom; memory limit =
2 x observed peak. Current chart requests: usage-api 100m / 128Mi, notification-service 50m / 96Mi, loadgen 50m / 64Mi.
Record the measured values in the table in section 4 and adjust `values-*.yaml` with the evidence.

For RDS, use the CloudWatch dashboard (`telecom-dev-aws-services`): CPU and connections under 20 % at the demo load mean
the smallest class (db.t4g.micro) is right for dev.

## 3. Optimization actions (implemented, with the evidence to show)

| # | Action | Where | Saving / effect | Evidence |
|---|---|---|---|---|
| 1 | **One shared NAT gateway in dev** (one per AZ only in prod) and a **free S3 gateway endpoint** so ECR image layers and S3 traffic skip the NAT | `terraform/modules/network`, `envs/*.tfvars` | ~$33 / month per avoided NAT, plus $0.045 / GB of NAT processing for image pulls | VPC console (1 NAT in dev), Cost Explorer "NAT Gateway" line, `BytesOutToDestination` on the NAT before/after |
| 2 | **Retention on every log and telemetry store**: CloudWatch log groups created by Terraform with 14 days (dev) / 30 days (prod) instead of "never expire" (the Container Insights default), Prometheus 7 days / 8 GB, Tempo 72 h, DEBUG logs only in dev | `terraform/modules/*`, `observability/k8s/*.yaml`, `values-*.yaml` | Log storage stops growing linearly; ingestion is the remaining cost driver | CloudWatch Logs console: retention column; `aws logs describe-log-groups --query 'logGroups[].[logGroupName,retentionInDays,storedBytes]'` |
| 3 | **Lifecycle rules**: ECR keeps the last 20 images and expires untagged ones after 7 days; CloudTrail S3 objects expire after 90 days (dev); old state versions expire after 90 days | `modules/ecr`, `modules/security`, `bootstrap` | Storage bounded instead of growing with every build | ECR repository > Lifecycle policy, S3 bucket > Management |
| 4 | **Autoscaling instead of peak sizing**: HPA on usage-api (dev 1-3, prod 2-6 replicas at 70 % CPU); RDS storage autoscaling (20 -> 50 GiB) instead of over-provisioned disk; gp3 everywhere | chart, `modules/rds` | Pay for average, not peak, load | `kubectl get hpa -w` during a load test; RDS "Storage autoscaling" setting |
| 5 | **Graviton and burstable classes where load is low**: db.t4g (ARM) for RDS | `envs/*.tfvars` | ~10-20 % cheaper than the x86 equivalent | tfvars, RDS console |
| 6 | **Switch dev off when not used** (largest single lever): `terraform destroy` after each session, or scale the node group to 0 and stop RDS overnight | `terraform/README.md` | ~70 % of the dev bill (EKS control plane remains if not destroyed) | Cost Explorer daily view shows the gaps |
| 7 | Optional: **SPOT nodes for dev** (`node_capacity_type = "SPOT"`) | `envs/dev.tfvars` | ~60-70 % on EC2 | Node group capacity type |

### Before / after table (fill with measurements)

| Metric | Before | After | Source |
|---|---|---|---|
| Monthly estimate, dev | | | Cost Explorer / calculator |
| NAT gateways in dev | 2 (one per AZ) | 1 | VPC console |
| Log groups with "never expire" | | 0 | `describe-log-groups` |
| CPU requested vs used (usage-api) | | | Prometheus queries above |
| ECR images stored per repository | | <= 20 | ECR console |

## 4. Telemetry cost review (Day 4)

| Signal | Volume driver | Control in place | Further option |
|---|---|---|---|
| Container logs (CloudWatch) | log lines x pods; DEBUG in dev | JSON logs, probe/scrape requests logged at DEBUG only, retention | Infrequent Access log class for low-value groups; drop DEBUG in dev when not debugging |
| Control-plane logs | `audit` is the largest | retention | Keep only `audit` + `authenticator` outside incidents |
| VPC flow logs | traffic volume | retention, `enable_flow_logs` switch | Send to S3 instead of CloudWatch (cheaper storage) |
| Prometheus | series x scrape interval | 15 s scrape, 7 d retention, label cardinality bounded (route templates, not raw paths) | 30 s scrape in prod if not needed |
| Container Insights metrics | per node/pod metrics | add-on default | Disable enhanced observability if Prometheus covers the need |
| Traces (Tempo) | spans per request | 72 h retention, dev only | Sampling (`OTEL_TRACES_SAMPLER=parentbased_traceidratio`) |

## 5. Migration view (local stack -> EKS)

The platform was first built and tested locally (docker-compose: PostgreSQL, LocalStack SQS, Prometheus, Grafana) and
then moved to AWS. Waves, validation and rollback for that move:

| Wave | Move | Validation | Rollback |
|---|---|---|---|
| 0 | Terraform state bucket, network, KMS | `terraform plan` clean, VPC reachable | `terraform destroy` (nothing depends on it yet) |
| 1 | EKS, ECR, add-ons (ALB controller, Prometheus/Grafana, Container Insights) | nodes Ready, Grafana up, logs arriving | destroy wave 1 |
| 2 | Data layer: RDS, SQS + DLQ, secrets | `/readyz` shows database and queue ok | keep running locally; data layer has no users yet |
| 3 | Application via CI (`deploy dev`) | smoke test, SLO dashboards green, alerts quiet for 30 min | `helm rollback` (automatic on failed rollout) |
| 4 | Failure tests on EKS (docs/failure-scenarios.md) | alert fired, layer identified, recovery measured | `POST /chaos/reset` |
