variable "name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "allowed_security_group_ids" {
  description = "Security groups allowed to connect on 5432 (the EKS node security group)."
  type        = list(string)
}

variable "kms_key_arn" {
  type = string
}

variable "instance_class" {
  type = string
}

variable "allocated_storage" {
  type = number
}

variable "max_allocated_storage" {
  type = number
}

variable "multi_az" {
  type = bool
}

variable "backup_retention_days" {
  type = number
}

variable "deletion_protection" {
  type = bool
}

variable "skip_final_snapshot" {
  type = bool
}

variable "performance_insights" {
  type = bool
}

variable "apply_immediately" {
  type    = bool
  default = false
}

variable "log_retention_days" {
  type = number
}

variable "secret_recovery_days" {
  type = number
}

variable "db_name" {
  type    = string
  default = "usage"
}

variable "username" {
  type    = string
  default = "usageadmin"
}
