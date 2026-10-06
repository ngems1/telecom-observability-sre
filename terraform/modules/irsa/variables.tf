variable "name" {
  description = "IAM role name."
  type        = string
}

variable "oidc_provider_arn" {
  type = string
}

variable "oidc_issuer_url" {
  description = "Cluster OIDC issuer URL (with or without https://)."
  type        = string
}

variable "namespace" {
  type = string
}

variable "service_account" {
  type = string
}

variable "managed_policy_arns" {
  description = "Static AWS-managed policy ARNs to attach."
  type        = list(string)
  default     = []
}

variable "has_inline_policy" {
  description = "Set true when inline_policy_json is provided (a plain bool keeps count known at plan time)."
  type        = bool
  default     = false
}

variable "inline_policy_json" {
  type    = string
  default = ""
}
