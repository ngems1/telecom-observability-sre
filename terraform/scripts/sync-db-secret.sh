#!/usr/bin/env bash
# Copies the database URL from AWS Secrets Manager into the Kubernetes Secret the chart expects
# (usage-db-credentials / key DATABASE_URL). The password is never written to a file or to git.
#   ./terraform/scripts/sync-db-secret.sh [dev|prod]
set -euo pipefail

ENVIRONMENT="${1:-dev}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGION="$(terraform -chdir="$ROOT" output -raw region)"
SECRET_NAME="$(terraform -chdir="$ROOT" output -raw database_url_secret_name)"
NAMESPACE="$(terraform -chdir="$ROOT" output -raw namespace)"

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

DATABASE_URL="$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET_NAME" \
  --query SecretString --output text)"

kubectl -n "$NAMESPACE" create secret generic usage-db-credentials \
  --from-literal=DATABASE_URL="$DATABASE_URL" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Secret usage-db-credentials updated in namespace ${NAMESPACE} (${ENVIRONMENT})."
echo "Restart usage-api to pick up a changed password: kubectl -n ${NAMESPACE} rollout restart deploy/usage-api"
