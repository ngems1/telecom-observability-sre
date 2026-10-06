variable "name" {
  description = "Name prefix, e.g. telecom-dev."
  type        = string
}

variable "environment" {
  description = "GitHub Environment name the deploy job must target (dev or prod)."
  type        = string
}

variable "repository" {
  description = "owner/name of the GitHub repository."
  type        = string
}

variable "create_provider" {
  type    = bool
  default = true
}

variable "existing_provider_arn" {
  description = "ARN of an existing GitHub OIDC provider when create_provider is false."
  type        = string
  default     = ""
}

variable "ecr_repository_arns" {
  type = list(string)
}

variable "cluster_name" {
  type = string
}

variable "cluster_arn" {
  type = string
}

variable "helm_values_parameter_arn" {
  description = "SSM parameter holding the environment's Helm values; the deploy role may read it."
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace the deploy role may edit."
  type        = string
}
