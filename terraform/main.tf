data "aws_caller_identity" "current" {}

locals {
  name      = "${var.project}-${var.environment}"
  namespace = "telecom-${var.environment}"

  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
    Repository  = var.github_repository
  }

  account_id   = data.aws_caller_identity.current.account_id
  ecr_registry = "${local.account_id}.dkr.ecr.${var.region}.amazonaws.com"
  services     = ["usage-api", "notification-service"]
  repositories = [for s in local.services : "${var.project}/${s}"]
}

# ------------------------------------------------------------ foundations
module "kms" {
  source = "./modules/kms"

  name   = local.name
  region = var.region
}

module "network" {
  source = "./modules/network"

  name               = local.name
  region             = var.region
  vpc_cidr           = var.vpc_cidr
  az_count           = var.az_count
  single_nat_gateway = var.single_nat_gateway
  enable_flow_logs   = var.enable_flow_logs
  log_retention_days = var.log_retention_days
  kms_key_arn        = module.kms.key_arn
}

# ------------------------------------------------------------ EKS (#9)
module "eks" {
  source = "./modules/eks"

  name                 = local.name
  kubernetes_version   = var.kubernetes_version
  subnet_ids           = module.network.private_subnet_ids
  kms_key_arn          = module.kms.key_arn
  log_retention_days   = var.log_retention_days
  public_access_cidrs  = var.api_allowed_cidrs
  admin_principal_arns = var.admin_principal_arns
  node_instance_types  = var.node_instance_types
  node_capacity_type   = var.node_capacity_type
  node_min_size        = var.node_min_size
  node_desired_size    = var.node_desired_size
  node_max_size        = var.node_max_size
  node_disk_gb         = var.node_disk_gb
}

# Images are built once and promoted dev -> prod (same SHA tag), so the repositories are shared:
# the dev stack creates them; a prod stack in the same account looks them up instead.
module "ecr" {
  source = "./modules/ecr"
  count  = var.create_ecr_repositories ? 1 : 0

  repositories = local.repositories
  kms_key_arn  = module.kms.key_arn
  force_delete = var.environment == "dev"
}

data "aws_ecr_repository" "existing" {
  for_each = var.create_ecr_repositories ? toset([]) : toset(local.repositories)

  name = each.value
}

locals {
  ecr_repository_arns = tolist(var.create_ecr_repositories ? module.ecr[0].repository_arns : [for r in data.aws_ecr_repository.existing : r.arn])
  ecr_repository_urls = tomap(var.create_ecr_repositories ? module.ecr[0].repository_urls : { for k, r in data.aws_ecr_repository.existing : k => r.repository_url })
}

# ------------------------------------------------------------ data layer
module "sqs" {
  source = "./modules/sqs"

  queue_name  = "usage-alerts-${var.environment}"
  dlq_name    = "usage-alerts-dlq-${var.environment}"
  kms_key_arn = module.kms.key_arn
}

module "rds" {
  source = "./modules/rds"

  name                       = "${local.name}-usage"
  vpc_id                     = module.network.vpc_id
  subnet_ids                 = module.network.private_subnet_ids
  allowed_security_group_ids = [module.eks.cluster_security_group_id]
  kms_key_arn                = module.kms.key_arn
  instance_class             = var.db_instance_class
  allocated_storage          = var.db_allocated_storage
  max_allocated_storage      = var.db_max_allocated_storage
  multi_az                   = var.db_multi_az
  backup_retention_days      = var.db_backup_retention_days
  deletion_protection        = var.db_deletion_protection
  skip_final_snapshot        = var.db_skip_final_snapshot
  performance_insights       = var.db_performance_insights
  log_retention_days         = var.log_retention_days
  secret_recovery_days       = var.secret_recovery_days
}

# ------------------------------------------------------------ pod identities (IRSA, least privilege) (#11)
data "aws_iam_policy_document" "usage_api" {
  statement {
    sid       = "PublishAlertEvents"
    actions   = ["sqs:SendMessage", "sqs:GetQueueUrl", "sqs:GetQueueAttributes"]
    resources = [module.sqs.queue_arn]
  }
  statement {
    sid       = "UseQueueKey"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [module.kms.key_arn]
  }
}

data "aws_iam_policy_document" "notification_service" {
  statement {
    sid = "ConsumeAlertEvents"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueUrl",
      "sqs:GetQueueAttributes",
    ]
    resources = [module.sqs.queue_arn]
  }
  statement {
    sid       = "UseQueueKey"
    actions   = ["kms:Decrypt"]
    resources = [module.kms.key_arn]
  }
}

module "usage_api_irsa" {
  source = "./modules/irsa"

  name               = "${local.name}-usage-api"
  oidc_provider_arn  = module.eks.oidc_provider_arn
  oidc_issuer_url    = module.eks.oidc_issuer_url
  namespace          = local.namespace
  service_account    = "usage-api"
  has_inline_policy  = true
  inline_policy_json = data.aws_iam_policy_document.usage_api.json
}

module "notification_service_irsa" {
  source = "./modules/irsa"

  name               = "${local.name}-notification-service"
  oidc_provider_arn  = module.eks.oidc_provider_arn
  oidc_issuer_url    = module.eks.oidc_issuer_url
  namespace          = local.namespace
  service_account    = "notification-service"
  has_inline_policy  = true
  inline_policy_json = data.aws_iam_policy_document.notification_service.json
}

# AWS Load Balancer Controller (creates the ALB from the chart's Ingress). Policy is the upstream one,
# kept in policies/ (re-download it when you upgrade the controller).
module "lb_controller_irsa" {
  source = "./modules/irsa"

  name               = "${local.name}-aws-lb-controller"
  oidc_provider_arn  = module.eks.oidc_provider_arn
  oidc_issuer_url    = module.eks.oidc_issuer_url
  namespace          = "kube-system"
  service_account    = "aws-load-balancer-controller"
  has_inline_policy  = true
  inline_policy_json = file("${path.module}/policies/aws-load-balancer-controller.json")
}

# Alertmanager (kube-prometheus-stack) publishes Prometheus SLO alerts to the same SNS topic as the
# CloudWatch alarms, so every alert reaches the on-call channel the same way.
data "aws_iam_policy_document" "alertmanager" {
  statement {
    sid       = "PublishAlerts"
    actions   = ["sns:Publish"]
    resources = [module.observability.sns_topic_arn]
  }
  statement {
    sid       = "UseTopicKey"
    actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
    resources = [module.kms.key_arn]
  }
}

module "alertmanager_irsa" {
  source = "./modules/irsa"

  name               = "${local.name}-alertmanager"
  oidc_provider_arn  = module.eks.oidc_provider_arn
  oidc_issuer_url    = module.eks.oidc_issuer_url
  namespace          = "monitoring"
  service_account    = "alertmanager"
  has_inline_policy  = true
  inline_policy_json = data.aws_iam_policy_document.alertmanager.json
}

# ------------------------------------------------------------ Helm values for this environment
# The same document is exposed as an output (for humans) and stored in SSM Parameter Store, where the
# GitHub Actions deploy job reads it. Nothing is copied by hand between Terraform and Helm.
locals {
  helm_values = yamlencode({
    global = {
      environment = var.environment
      region      = var.region
    }
    image = {
      registry = local.ecr_registry
    }
    networkPolicy = {
      albSourceCidrs = module.network.public_subnet_cidrs
    }
    usageApi = {
      serviceAccount = {
        annotations = { "eks.amazonaws.com/role-arn" = module.usage_api_irsa.role_arn }
      }
      config = {
        sqsQueueUrl = module.sqs.queue_url
      }
      ingress = {
        enabled   = true
        className = "alb"
        annotations = {
          "alb.ingress.kubernetes.io/scheme" = "internet-facing"
        }
      }
    }
    notificationService = {
      serviceAccount = {
        annotations = { "eks.amazonaws.com/role-arn" = module.notification_service_irsa.role_arn }
      }
      config = {
        sqsQueueUrl = module.sqs.queue_url
      }
    }
  })
}

resource "aws_ssm_parameter" "helm_values" {
  name        = "/${var.project}/${var.environment}/helm-values"
  description = "Helm values wiring charts/telecom-app to the ${var.environment} environment (no secrets)"
  type        = "String"
  value       = local.helm_values
}

# ------------------------------------------------------------ CI/CD identity (GitHub OIDC)
module "github_oidc" {
  source = "./modules/github_oidc"

  name                      = local.name
  environment               = var.environment
  repository                = var.github_repository
  create_provider           = var.create_github_oidc_provider
  existing_provider_arn     = var.github_oidc_provider_arn
  ecr_repository_arns       = local.ecr_repository_arns
  cluster_name              = module.eks.cluster_name
  cluster_arn               = module.eks.cluster_arn
  namespace                 = local.namespace
  helm_values_parameter_arn = aws_ssm_parameter.helm_values.arn
}

# ------------------------------------------------------------ CloudWatch logs, alarms, dashboard (#10)
module "observability" {
  source = "./modules/observability"

  name                  = local.name
  region                = var.region
  cluster_name          = module.eks.cluster_name
  kms_key_arn           = module.kms.key_arn
  log_retention_days    = var.log_retention_days
  alert_email           = var.alert_email
  queue_name            = module.sqs.queue_name
  dlq_name              = module.sqs.dlq_name
  db_identifier         = module.rds.identifier
  sqs_alarm_age_seconds = var.sqs_alarm_age_seconds
}

# ------------------------------------------------------------ security services (#11)
module "security" {
  source = "./modules/security"

  name                  = local.name
  region                = var.region
  kms_key_arn           = module.kms.key_arn
  log_retention_days    = var.log_retention_days
  trail_retention_days  = var.cloudtrail_retention_days
  force_destroy_buckets = var.environment == "dev"
  enable_cloudtrail     = var.security_services.cloudtrail
  enable_guardduty      = var.security_services.guardduty
  enable_inspector      = var.security_services.inspector
  enable_securityhub    = var.security_services.securityhub
}
