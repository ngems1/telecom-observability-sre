variable "project" {
  description = "Short project name used as a prefix for resource names and ECR repositories."
  type        = string
  default     = "telecom"
}

variable "environment" {
  description = "Environment name (dev or prod). Drives names, the Kubernetes namespace (telecom-<env>) and sizing defaults in envs/*.tfvars."
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be dev or prod."
  }
}

variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

# ---------------------------------------------------------------- network
variable "vpc_cidr" {
  description = "VPC CIDR block."
  type        = string
  default     = "10.20.0.0/16"
}

variable "az_count" {
  description = "Number of availability zones (2 minimum: RDS subnet groups need two)."
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3."
  }
}

variable "single_nat_gateway" {
  description = "One shared NAT gateway (cheap, single AZ failure domain) instead of one per AZ. true for dev, false for prod."
  type        = bool
  default     = true
}

variable "enable_flow_logs" {
  description = "Send VPC flow logs to CloudWatch Logs (security evidence; costs ingestion)."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------- EKS
variable "kubernetes_version" {
  description = "EKS Kubernetes version. Check `aws eks describe-cluster-versions` for versions still in standard support before applying."
  type        = string
  default     = "1.34"
}

variable "api_allowed_cidrs" {
  description = "CIDRs allowed to reach the public Kubernetes API endpoint. Restrict to your IP (x.x.x.x/32) where possible; GitHub-hosted runners need it open (IAM still authenticates every call)."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "admin_principal_arns" {
  description = "Extra IAM role/user ARNs given cluster-admin through EKS access entries (the identity running terraform is admin automatically)."
  type        = list(string)
  default     = []
}

variable "node_instance_types" {
  description = "Instance types for the managed node group."
  type        = list(string)
  default     = ["t3.large"]
}

variable "node_capacity_type" {
  description = "ON_DEMAND or SPOT. SPOT cuts compute cost ~60-70% and suits dev."
  type        = string
  default     = "ON_DEMAND"
}

variable "node_min_size" {
  type    = number
  default = 2
}

variable "node_desired_size" {
  type    = number
  default = 2
}

variable "node_max_size" {
  type    = number
  default = 4
}

variable "node_disk_gb" {
  description = "Node root volume size (gp3, encrypted)."
  type        = number
  default     = 30
}

# ---------------------------------------------------------------- RDS
variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_allocated_storage" {
  type    = number
  default = 20
}

variable "db_max_allocated_storage" {
  description = "Storage autoscaling ceiling in GiB (0 disables autoscaling)."
  type        = number
  default     = 50
}

variable "db_multi_az" {
  type    = bool
  default = false
}

variable "db_backup_retention_days" {
  type    = number
  default = 3
}

variable "db_deletion_protection" {
  type    = bool
  default = false
}

variable "db_skip_final_snapshot" {
  type    = bool
  default = true
}

variable "db_performance_insights" {
  description = "Enable Performance Insights (not supported on the smallest instance classes)."
  type        = bool
  default     = false
}

variable "secret_recovery_days" {
  description = "Secrets Manager recovery window. 0 deletes immediately (dev teardown); use 7-30 in prod."
  type        = number
  default     = 0
}

# ---------------------------------------------------------------- observability / security
variable "log_retention_days" {
  description = "Retention for every CloudWatch log group created here. Must be a value CloudWatch accepts (1, 3, 5, 7, 14, 30, 60, 90, ...)."
  type        = number
  default     = 14
}

variable "alert_email" {
  description = "Email subscribed to the alarm SNS topic (confirm the subscription email). Empty = no subscription."
  type        = string
  default     = ""
}

variable "sqs_alarm_age_seconds" {
  description = "Alarm when the oldest queued message is older than this (stuck-consumer signal)."
  type        = number
  default     = 120
}

variable "security_services" {
  description = "Account-level security services to manage. Set one to false if it is already enabled in the account (or import it)."
  type = object({
    cloudtrail  = bool
    guardduty   = bool
    inspector   = bool
    securityhub = bool
  })
  default = {
    cloudtrail  = true
    guardduty   = true
    inspector   = true
    securityhub = true
  }
}

variable "cloudtrail_retention_days" {
  description = "Days to keep CloudTrail log files in S3 before expiry."
  type        = number
  default     = 90
}

# ---------------------------------------------------------------- CI/CD
variable "github_repository" {
  description = "GitHub repository (owner/name) allowed to assume the CI roles through OIDC."
  type        = string
  default     = "ngems1/telecom-observability-sre"
}

variable "create_ecr_repositories" {
  description = "Create the shared ECR repositories. true in the first (dev) stack; false for another stack in the same account, which looks them up."
  type        = bool
  default     = true
}

variable "create_github_oidc_provider" {
  description = "Create the GitHub OIDC identity provider. Only one can exist per AWS account; set false if it already does."
  type        = bool
  default     = true
}

variable "github_oidc_provider_arn" {
  description = "ARN of the existing GitHub OIDC provider; used only when create_github_oidc_provider is false."
  type        = string
  default     = ""
}
