output "cluster_name" {
  value = module.eks.cluster_name
}

output "region" {
  value = var.region
}

output "namespace" {
  description = "Kubernetes namespace the Helm release should use."
  value       = local.namespace
}

output "ecr_registry" {
  value = local.ecr_registry
}

output "ecr_repository_urls" {
  value = local.ecr_repository_urls
}

output "sqs_queue_url" {
  value = module.sqs.queue_url
}

output "sqs_dlq_url" {
  value = module.sqs.dlq_url
}

output "database_url_secret_name" {
  description = "Secrets Manager secret holding the SQLAlchemy URL (see scripts/sync-db-secret.sh)."
  value       = module.rds.database_url_secret_name
}

output "alarm_topic_arn" {
  value = module.observability.sns_topic_arn
}

output "cloudwatch_dashboard" {
  value = module.observability.dashboard_name
}

output "github_ecr_push_role_arn" {
  description = "Set as the AWS_ECR_PUSH_ROLE_ARN repository variable used by the GitHub Actions build job."
  value       = module.github_oidc.push_role_arn
}

output "github_deploy_role_arn" {
  description = "Set as AWS_DEPLOY_ROLE_ARN on the GitHub Environment matching this environment."
  value       = module.github_oidc.deploy_role_arn
}

output "aws_lb_controller_role_arn" {
  value = module.lb_controller_irsa.role_arn
}

output "vpc_id" {
  value = module.network.vpc_id
}

# Save with:  terraform output -raw helm_values > values-aws.generated.yaml
# then:       helm upgrade --install telecom charts/telecom-app -n <namespace> -f charts/telecom-app/values-<env>.yaml -f values-aws.generated.yaml --set usageApi.image.tag=<sha> --set notificationService.image.tag=<sha>
# The CI deploy job reads the same document from SSM (helm_values_parameter).
output "helm_values" {
  description = "Helm values wiring the chart to this environment's registry, queue, IAM roles and network."
  value       = local.helm_values
}

output "helm_values_parameter" {
  description = "SSM parameter holding helm_values (read by the GitHub Actions deploy job)."
  value       = aws_ssm_parameter.helm_values.name
}

output "alertmanager_role_arn" {
  value = module.alertmanager_irsa.role_arn
}

output "application_log_group" {
  description = "Container logs of both services (Logs Insights saved queries are under <name>/ in the console)."
  value       = module.observability.application_log_group
}
