# Terraform: AWS infrastructure for the telecom platform

Everything the Helm chart needs to run on AWS, as code and repeatable: network, EKS, ECR, SQS (+ DLQ), RDS PostgreSQL,
pod IAM roles (IRSA), GitHub OIDC for CI/CD, CloudWatch logs/alarms/dashboard and the AWS security services.

> **Status:** written and cross-checked statically (every variable, resource, output and module input resolves; see
> "How this was checked"). It has **not yet been through `terraform validate` / `plan` / `apply`** because the
> authoring environment could not download Terraform. Run the first plan yourself and send me anything it flags.

## What it creates

| Module | Resources | Brief topic |
|---|---|---|
| `kms` | One customer-managed key (rotation on) for SQS, RDS, Secrets Manager, SNS, logs, CloudTrail | #11 |
| `network` | VPC, public/private subnets over 2-3 AZs, NAT (1 shared in dev, 1 per AZ in prod), free S3 gateway endpoint, flow logs, default SG locked down | #9 |
| `eks` | Cluster (KMS secret encryption, all 5 control-plane log types, access entries instead of aws-auth), managed node group (IMDSv2, encrypted gp3), OIDC provider, add-ons: vpc-cni, kube-proxy, coredns, EBS CSI, **Container Insights + Fluent Bit** | #9, #10 |
| `ecr` | `telecom/usage-api`, `telecom/notification-service`: immutable tags, scan on push, KMS, lifecycle (untagged 7 days, keep last 20) | #9, #12 |
| `sqs` | `usage-alerts-<env>` + DLQ (redrive after 3 receives), SSE-KMS, TLS-only policy | #10 |
| `rds` | PostgreSQL 16 in private subnets, SG open only to the EKS nodes, KMS, forced TLS, slow-query + engine logs to CloudWatch, backups; DB URL in Secrets Manager | #9, #11 |
| `irsa` | One IAM role per service account: `usage-api` (send), `notification-service` (consume), AWS Load Balancer Controller, EBS CSI, CloudWatch agent | #11 |
| `github_oidc` | GitHub OIDC provider + two roles: **ecr-push** (main branch only) and **deploy** (only jobs targeting the matching GitHub Environment; namespace-scoped Kubernetes rights). No AWS keys in GitHub | #11 |
| `observability` | Log groups with retention, SNS topic (+ email), alarms for SQS age / DLQ / backlog, RDS CPU / storage / connections, failed nodes (each tagged `severity` and `owner`), CloudWatch dashboard | #10 |
| `security` | CloudTrail (multi-region, validated, KMS, S3 lifecycle, to CloudWatch Logs), GuardDuty (+ EKS audit logs), Inspector (ECR + EC2), Security Hub (AWS Foundational Best Practices) | #11 |

`terraform output -raw helm_values` produces a Helm values file wiring the chart to this environment (ECR registry, queue URL,
IRSA role annotations, ALB Ingress annotations), so nothing is copied by hand.

## Prerequisites

Terraform >= 1.10, AWS CLI v2, kubectl, Helm 3, and AWS credentials with admin rights in a **sandbox/training account**
(`aws sts get-caller-identity` should show it). Scripts are bash: use WSL or Git Bash on Windows.

## First deployment

```bash
# 1. State bucket (once per account). Local state, tiny stack.
terraform -chdir=terraform/bootstrap init
terraform -chdir=terraform/bootstrap apply
terraform -chdir=terraform/bootstrap output state_bucket     # note the bucket name

# 2. Main stack. Copy the backend example, put the bucket name in it.
cp terraform/envs/backend-dev.hcl.example terraform/envs/backend-dev.hcl
terraform -chdir=terraform init -backend-config=envs/backend-dev.hcl
terraform -chdir=terraform plan  -var-file=envs/dev.tfvars -out=dev.tfplan
terraform -chdir=terraform apply dev.tfplan                   # ~15-20 min (EKS + RDS)

# 3. In-cluster add-ons (metrics-server, ALB controller, Prometheus/Grafana), DB secret, application
./terraform/scripts/install-addons.sh dev
./terraform/scripts/sync-db-secret.sh dev
terraform -chdir=terraform output -raw helm_values > values-aws.generated.yaml
helm upgrade --install telecom charts/telecom-app -n telecom-dev --create-namespace \
  -f charts/telecom-app/values-dev.yaml -f values-aws.generated.yaml \
  --set usageApi.image.tag=<git-sha> --set notificationService.image.tag=<git-sha>
```

Images must be in ECR before the pods start: until the CI pipeline exists, build and push by hand
(`aws ecr get-login-password | docker login ...`, then `docker build` and `docker push` to
`terraform output ecr_registry`).

Before the first `apply`, set in `envs/dev.tfvars`: `alert_email`, and restrict `api_allowed_cidrs` to your IP if you can.

## Settings you will likely change

| Variable | Why |
|---|---|
| `security_services` | GuardDuty, Inspector, Security Hub and CloudTrail are one-per-account/region. If one is already enabled, set it to `false` (or `terraform import` it) or apply fails with "already exists". |
| `create_github_oidc_provider` | Only one GitHub OIDC provider per account. Set `false` if it exists, and set `github_oidc_provider_arn` to its ARN. |
| `kubernetes_version` | Default `1.34`. Confirm the version is in standard support in your region before applying. |
| `node_capacity_type` | `SPOT` cuts compute roughly 60-70% in dev; nodes can be reclaimed mid-demo. |
| `admin_principal_arns` | Extra IAM roles/users with cluster-admin. The identity that runs Terraform is already admin; do not list it again. |

## Cost (rough, us-east-1, always-on dev; check the AWS pricing calculator for current numbers)

| Item | Approx. per month |
|---|---|
| EKS control plane | $73 |
| 2 x t3.large nodes | $120 |
| 1 NAT gateway (+ data) | $33+ |
| Application Load Balancer | $17+ |
| RDS db.t4g.micro + 20 GiB | $15 |
| EBS volumes, CloudWatch logs/metrics, KMS, GuardDuty/Inspector after free trials | $15-40 |
| **Total** | **roughly $270** (about $9 per day) |

For a course project, **destroy the stack after each work session**: that turns this into a few dollars per session.
Cost levers already in the code, and what to cite as optimization evidence in your cost analysis: single NAT in dev,
S3 gateway endpoint (keeps ECR layer pulls off the NAT), log retention on every log group (14 days dev), ECR lifecycle
rules, CloudTrail S3 lifecycle, gp3 volumes, RDS storage autoscaling instead of over-provisioning, SPOT option, HPA on the API.

## Teardown

```bash
helm uninstall telecom -n telecom-dev          # removes the Ingress, which deletes the ALB
helm uninstall aws-load-balancer-controller -n kube-system
helm uninstall kube-prometheus-stack -n monitoring   # releases the EBS volumes
terraform -chdir=terraform destroy -var-file=envs/dev.tfvars
```

Do the Helm steps first: an ALB or EBS volume left behind blocks VPC deletion. The state bucket (`bootstrap`) is kept
on purpose; delete it last by emptying it and running `terraform destroy` in `bootstrap/`.

## Security notes and honest limits

- **The DB password is in Terraform state** (generated by `random_password`). State is private, versioned and
  KMS-encrypted, but treat it as sensitive. For prod, move to RDS-managed master passwords with rotation.
- **Public Kubernetes API endpoint** (needed for GitHub-hosted runners to deploy). Every call is IAM-authenticated and
  CIDRs are configurable; a private endpoint plus self-hosted runners is the hardened option.
- **ALB is HTTP-only** in this lab (no domain). For real use add an ACM certificate and `alb.ingress.kubernetes.io/certificate-arn`.
- **Deploy role** uses the namespace-scoped `AmazonEKSEditPolicy`. If `helm upgrade` is denied on `ServiceMonitor` or
  `PrometheusRule`, switch the association in `modules/github_oidc` to `AmazonEKSAdminPolicy` (still namespace-scoped).
- **Security Hub** controls that need AWS Config only report if Config recording is enabled (not included here).
- **Not included:** WAF, VPC interface endpoints, EKS node autoscaling (Cluster Autoscaler/Karpenter), External Secrets Operator
  (the DB secret is synced by script), Alertmanager receivers, and RDS enhanced monitoring. Each is a reasonable "next step" slide.

## How this was checked

`terraform fmt`, `validate`, `tflint` and `checkov` could not be run where this was written. Instead a script verified that
every file's delimiters balance, every `var.*`, `local.*`, `data.*`, resource and `module.*.output` reference resolves,
every module call supplies all required inputs and no unknown ones, `*.tfvars` only set declared variables, and the
ALB controller policy is valid JSON. That catches wiring mistakes, **not** provider-specific argument errors. The CI pipeline
should run `terraform fmt -check`, `terraform validate` and Checkov on this directory, and you should expect to fix a
small number of issues on the first `plan`.
