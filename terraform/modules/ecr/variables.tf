variable "repositories" {
  description = "Repository names, e.g. [\"telecom/usage-api\", \"telecom/notification-service\"]."
  type        = list(string)
}

variable "kms_key_arn" {
  type = string
}

variable "keep_last_images" {
  type    = number
  default = 20
}

variable "force_delete" {
  description = "Allow terraform destroy to delete repositories that still hold images (dev only)."
  type        = bool
  default     = false
}
