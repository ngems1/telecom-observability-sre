variable "name" {
  description = "Name prefix; also the EKS cluster name used in subnet discovery tags."
  type        = string
}

variable "region" {
  type = string
}

variable "vpc_cidr" {
  type = string
}

variable "az_count" {
  type = number
}

variable "single_nat_gateway" {
  type = bool
}

variable "enable_flow_logs" {
  type = bool
}

variable "log_retention_days" {
  type = number
}

variable "kms_key_arn" {
  description = "Key used to encrypt the flow log group."
  type        = string
}
