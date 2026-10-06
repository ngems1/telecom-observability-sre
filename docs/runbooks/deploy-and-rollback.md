# Runbook: deploy and roll back

Owner: platform-sre. Applies to every environment (`dev`, `prod`).

## One-time setup

1. **Infrastructure** (see `terraform/README.md`): bootstrap the state bucket, `terraform apply` the environment,
   run `terraform/scripts/install-addons.sh <env>` and `terraform/scripts/sync-db-secret.sh <env>` (creates the namespace
   and the database Secret; the CI deploy role cannot create namespaces).
2. **GitHub repository variables** (Settings > Secrets and variables > Actions > Variables):
   - `AWS_REGION` = `us-east-1`
   - `AWS_ECR_PUSH_ROLE_ARN` = `terraform output -raw github_ecr_push_role_arn`
3. **GitHub Environments** (Settings > Environments): create `dev` (and `prod`), each with variable
   `AWS_DEPLOY_ROLE_ARN` = `terraform output -raw github_deploy_role_arn` of that environment.
   Add **required reviewers** to `prod` (and to `dev` if you want the approval step in the demo).
4. For prod promotion set repository variable `ENABLE_PROD_DEPLOY` = `true`.

No AWS access keys are stored in GitHub: the workflows use OIDC and the roles above.

## Normal deployment

Merge to `main`. The `ci-cd` workflow runs tests, lint, Helm and Terraform validation and the security scans, builds both
images tagged with the commit SHA, scans them, pushes them to ECR, then calls `deploy` for `dev`:

1. waits for Environment approval (if reviewers are configured),
2. reads `/telecom/dev/helm-values` from SSM (written by Terraform),
3. `helm upgrade --install --atomic --wait`: if the new pods do not become Ready within 10 minutes, Helm rolls back by itself,
4. runs `scripts/smoke-test.sh` inside the cluster; if it fails, the job rolls back to the previous revision and fails.

The job summary shows the deployed SHA, the Helm history and the API URL.

## Manual redeploy of a known version

Actions > **deploy** > Run workflow > environment + commit SHA (the images for that SHA must already be in ECR).

## Rollback

Pick the fastest that fits:

| Situation | Action | Time |
|---|---|---|
| Rollout fails readiness | Nothing: `--atomic` already rolled back; read the failed job log | automatic |
| Smoke test fails after deploy | Nothing: the deploy job rolled back; check the Summary | automatic |
| Problem found later (alerts, SLO burn) | Actions > **rollback** > environment (+ optional revision). Approval rules apply | ~2 min |
| GitHub unavailable | From a workstation with cluster access: `helm history telecom -n telecom-dev` then `helm rollback telecom <revision> -n telecom-dev --wait` and `./scripts/smoke-test.sh telecom-dev` | ~2 min |
| Bad infrastructure change | `git revert` the Terraform change, `terraform plan`, `terraform apply` | depends |

After any rollback: confirm on Grafana *Telecom - SLO Overview* that the burn rate falls, note the times
(detect / decide / recovered) and open a follow-up issue with the cause.

## Verifying a deployment by hand

```bash
kubectl -n telecom-dev get deploy,pods,hpa,ingress
helm -n telecom-dev history telecom
./scripts/smoke-test.sh telecom-dev
kubectl -n telecom-dev logs deploy/usage-api --tail=20
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `AccessDenied` on `AssumeRoleWithWebIdentity` | Environment name or branch does not match the role trust (`environment:dev`, `ref:refs/heads/main`) | Check the job's `environment`, and `github_repository` in tfvars |
| `helm upgrade` forbidden on `servicemonitors` or `prometheusrules` | Namespace-scoped Edit policy does not cover these CRDs | Switch the association in `terraform/modules/github_oidc` to `AmazonEKSAdminPolicy` (still namespace-scoped) |
| Pods `ImagePullBackOff` | Image for that SHA not in ECR (build job skipped push) | Check `AWS_ECR_PUSH_ROLE_ARN`; re-run `ci-cd` on main |
| Pods not Ready, `/readyz` says database unavailable | Secret missing or RDS security group | `sync-db-secret.sh`, check `kubectl get secret usage-db-credentials` |
| Ingress has no address | AWS Load Balancer Controller not installed or subnet tags missing | `kubectl -n kube-system logs deploy/aws-load-balancer-controller` |
| Everything unready right after enabling NetworkPolicy | Probes or Prometheus blocked | `kubectl get networkpolicy`; temporarily `--set networkPolicy.enabled=false` and redeploy |
