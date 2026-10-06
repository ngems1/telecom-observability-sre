variable "name" {
  type = string
}

variable "region" {
  type = string
}

variable "kms_key_arn" {
  type = string
}

variable "log_retention_days" {
  type = number
}

variable "trail_retention_days" {
  type = number
}

variable "force_destroy_buckets" {
  description = "Allow terraform destroy to empty and delete the CloudTrail bucket (dev only)."
  type        = bool
  default     = false
}

variable "enable_cloudtrail" {
  type = bool
}

variable "enable_guardduty" {
  type = bool
}

variable "enable_inspector" {
  type = bool
}

variable "enable_securityhub" {
  type = bool
}
