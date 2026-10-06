#!/usr/bin/env bash
# Installs the in-cluster add-ons Terraform does not manage, wiring them with `terraform output`:
#   - metrics-server              (HPA needs it; EKS does not ship it)
#   - AWS Load Balancer Controller (turns the chart's Ingress into an ALB)
#   - kube-prometheus-stack       (Prometheus, Alertmanager -> SNS, Grafana; picks up the app chart's
#                                  ServiceMonitors, PrometheusRule and dashboard ConfigMaps)
#   - Tempo                       (trace backend for OpenTelemetry; set INSTALL_TRACING=false to skip)
#
# Requires: aws, kubectl, helm, terraform. Run from the repository root:
#   ./terraform/scripts/install-addons.sh [dev|prod]
#
# Chart versions are not pinned by default (latest). After a good install, pin them for repeatability:
#   KPS_VERSION=<x.y.z> LBC_VERSION=<x.y.z> TEMPO_VERSION=<x.y.z> ./terraform/scripts/install-addons.sh dev
set -euo pipefail

ENVIRONMENT="${1:-dev}"
INSTALL_TRACING="${INSTALL_TRACING:-true}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
VALUES_DIR="$REPO/observability/k8s"
tf() { terraform -chdir="$ROOT" output -raw "$1"; }
version_flag() { [ -n "${1:-}" ] && printf -- '--version %s' "$1" || true; }

CLUSTER="$(tf cluster_name)"
REGION="$(tf region)"
VPC_ID="$(tf vpc_id)"
LB_ROLE_ARN="$(tf aws_lb_controller_role_arn)"
AM_ROLE_ARN="$(tf alertmanager_role_arn)"
TOPIC_ARN="$(tf alarm_topic_arn)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "==> kubeconfig for ${CLUSTER} (${ENVIRONMENT})"
aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER"

helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ >/dev/null
helm repo add eks https://aws.github.io/eks-charts >/dev/null
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
helm repo update >/dev/null

echo "==> metrics-server"
# shellcheck disable=SC2046
helm upgrade --install metrics-server metrics-server/metrics-server \
  --namespace kube-system $(version_flag "${METRICS_SERVER_VERSION:-}") --wait

echo "==> AWS Load Balancer Controller"
# shellcheck disable=SC2046
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  --namespace kube-system $(version_flag "${LBC_VERSION:-}") \
  --set clusterName="$CLUSTER" \
  --set region="$REGION" \
  --set vpcId="$VPC_ID" \
  --set serviceAccount.create=true \
  --set serviceAccount.name=aws-load-balancer-controller \
  --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=${LB_ROLE_ARN}" \
  --wait

echo "==> gp3 StorageClass (encrypted) for Prometheus, Grafana and Tempo volumes"
kubectl apply -f - <<'YAML'
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
provisioner: ebs.csi.aws.com
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
reclaimPolicy: Delete
parameters:
  type: gp3
  encrypted: "true"
YAML

# Environment-specific Alertmanager settings. Lists are replaced (not merged) by Helm, so the whole
# receivers list is restated here with the real topic ARN.
cat > "$TMP/alertmanager-env.yaml" <<YAML
alertmanager:
  serviceAccount:
    annotations:
      eks.amazonaws.com/role-arn: ${AM_ROLE_ARN}
  config:
    receivers:
      - name: "null"
      - name: sns
        sns_configs:
          - topic_arn: ${TOPIC_ARN}
            sigv4:
              region: ${REGION}
            send_resolved: true
YAML

echo "==> kube-prometheus-stack (Prometheus, Alertmanager -> SNS, Grafana)"
# shellcheck disable=SC2046
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace $(version_flag "${KPS_VERSION:-}") \
  -f "$VALUES_DIR/kube-prometheus-stack.yaml" \
  -f "$TMP/alertmanager-env.yaml" \
  --wait --timeout 10m

if [ "$INSTALL_TRACING" = "true" ]; then
  echo "==> Tempo (OpenTelemetry trace backend)"
  # shellcheck disable=SC2046
  helm upgrade --install tempo grafana/tempo \
    --namespace monitoring $(version_flag "${TEMPO_VERSION:-}") \
    -f "$VALUES_DIR/tempo.yaml" \
    --wait
fi

cat <<MSG

Add-ons are installed. Next:
  1. ./terraform/scripts/sync-db-secret.sh ${ENVIRONMENT}
  2. Deploy the app: push to main (GitHub Actions), or by hand:
       terraform -chdir=terraform output -raw helm_values > values-aws.generated.yaml
       helm upgrade --install telecom charts/telecom-app -n telecom-${ENVIRONMENT} --create-namespace \\
         -f charts/telecom-app/values-${ENVIRONMENT}.yaml -f values-aws.generated.yaml \\
         --set usageApi.image.tag=<git-sha> --set notificationService.image.tag=<git-sha>
  3. Grafana:  kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80
     user admin, password: kubectl -n monitoring get secret kube-prometheus-stack-grafana -o jsonpath='{.data.admin-password}' | base64 -d
  4. Confirm the SNS email subscription (sent to alert_email) so alerts reach you.
MSG
