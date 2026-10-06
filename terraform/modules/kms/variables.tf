variable "name" {
  description = "Name prefix, e.g. telecom-dev."
  type        = string
}

variable "region" {
  type = string
}

variable "deletion_window_days" {
  description = "Waiting period before a deleted key is destroyed (7-30)."
  type        = number
  default     = 7
}
