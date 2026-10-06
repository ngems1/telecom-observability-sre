# Final presentation: 15-minute plan

Matches brief section 11. Specialty = Observability / SRE, so it gets the deepest demo (sections 3 and 4).
Prepare everything in section "Before you start" so the live part is only clicks and two commands.

| Time | Section | What to show | Evidence / file |
|---|---|---|---|
| 0:00-2:00 | **Business problem and architecture** | The operator cannot tell which layer an outage comes from. One slide: the runtime diagram, and the layer -> signal table | `docs/architecture.md` sections 1 and 2 |
| 2:00-6:00 | **Live application / containers / EKS** | `kubectl get deploy,pods,hpa,pdb,networkpolicy -n telecom-dev`; one API call through the ALB with a correlation ID; the GitHub Actions run that deployed this SHA (tests, scans, OIDC, approval, smoke test) | `ci-cd` run page, `helm history telecom -n telecom-dev` |
| 6:00-9:00 | **Specialty: observability / SRE** | SLOs and why burn rates (99.9 % availability, 95 % under 300 ms); Grafana *SLO Overview* and *Service Triage*; follow one correlation ID across both services in Logs Insights, and the same request as a trace in Tempo | `charts/telecom-app/files/rules`, saved query `01-follow-a-correlation-id` |
| 9:00-12:00 | **Security + observability + failure scenario** | Live stuck-queue incident: inject, alert fires (Prometheus `NotificationConsumerStalled` + CloudWatch queue-age alarm, both to SNS email), triage dashboard isolates the layer while pods stay green, recover, give MTTD / MTTR. Then 30 s on security: IRSA, OIDC, NetworkPolicy, scan results | `docs/failure-scenarios.md` scenario 3, `docs/security.md` |
| 12:00-14:00 | **Migration / cost / optimization** | Baseline, then 3 actions with before/after numbers (single NAT + S3 endpoint, retention and lifecycle, autoscaling/right-sizing) | `docs/cost-optimization.md` |
| 14:00-15:00 | **Risks, lessons, next steps** | Pod health is not service health (alert on queue age), dual write -> outbox, Secrets rotation, WAF/HTTPS, KEDA for the consumer | `docs/security.md` section 5, README limitations |

## Before you start (30 minutes earlier)

- [ ] `terraform apply` done, add-ons installed, latest `main` deployed by CI, loadgen running in dev (5 rps).
- [ ] Grafana port-forward open: `kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80`.
- [ ] Port-forwards for the failure controls: `kubectl -n telecom-dev port-forward svc/notification-service 8081:8081`.
- [ ] SNS email subscription confirmed (test: `aws sns publish --topic-arn <alarm_topic_arn> --message test`).
- [ ] Dashboards at "Last 30 minutes", refresh 15 s. Alerts list empty (all green) before the incident.
- [ ] Screenshots as fallback: pipeline run, both dashboards, Security Hub score, Cost Explorer, Logs Insights result.

## Live incident script (3 minutes)

```bash
# T0: inject - the consumer stops, but its pod stays Running/Ready
curl -XPOST localhost:8081/chaos/stall -H 'content-type: application/json' -d '{"enabled": true}'
kubectl -n telecom-dev get pods            # all green: "Kubernetes says everything is fine"
# ~3 min: NotificationConsumerStalled (critical) fires; CloudWatch queue-age alarm follows; email arrives
# Triage dashboard: API fine, producer fine, consumer receive rate = 0 -> the consumer layer
curl -XPOST localhost:8081/chaos/reset     # recover; backlog drains, alerts resolve
```

Measured locally: alert went critical about 3 minutes after injection while `/healthz` and `/readyz` returned 200
the whole time. Use your EKS timings in the talk.

## Evidence checklist for the deliverables

| Deliverable (brief section 9) | Where |
|---|---|
| Architecture diagram | `docs/architecture.md` |
| Working EKS/container deployment | live demo + `ci-cd` run + `helm history` |
| Terraform/IaC code | `terraform/` |
| GitHub Actions workflow | `.github/workflows/ci-cd.yml`, `deploy.yml`, `rollback.yml` |
| Helm deployment artifacts | `charts/telecom-app/` |
| Security scan/findings report | `docs/security.md` + GitHub code scanning |
| CloudWatch and Prometheus/Grafana dashboards | Grafana *Telecom* folder, CloudWatch dashboard `telecom-dev-aws-services` |
| Cost/optimization analysis | `docs/cost-optimization.md` |
| Failure/recovery runbook | `docs/failure-scenarios.md`, `docs/runbooks/deploy-and-rollback.md` |
| README and final presentation | `README.md`, this outline |
