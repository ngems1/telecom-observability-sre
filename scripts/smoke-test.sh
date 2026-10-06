#!/usr/bin/env bash
# Post-deployment smoke test, run inside the cluster (no public endpoint needed):
# both Deployments rolled out, the API is ready and answers a real query, the consumer is ready.
#   ./scripts/smoke-test.sh telecom-dev
set -euo pipefail

NS="${1:?usage: smoke-test.sh <namespace>}"

kubectl -n "$NS" rollout status deploy/usage-api --timeout=5m
kubectl -n "$NS" rollout status deploy/notification-service --timeout=5m

kubectl -n "$NS" run "smoke-$(date +%s)" --rm -i --restart=Never --quiet \
  --image=curlimages/curl:8.10.1 --labels=app.kubernetes.io/name=smoke-test --command -- sh -ec '
  # Assign first: with `sh -e` a failing command substitution aborts on assignment, not inside echo arguments.
  api=$(curl -fsS --max-time 5 http://usage-api:8080/readyz)
  echo "usage-api /readyz:            $api"
  curl -fsS --max-time 5 -o /dev/null http://usage-api:8080/v1/plans
  echo "usage-api GET /v1/plans:      ok"
  worker=$(curl -fsS --max-time 5 http://notification-service:8081/readyz)
  echo "notification-service /readyz: $worker"
'
echo "Smoke test passed in namespace ${NS}"
