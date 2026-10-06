variable "queue_name" {
  type = string
}

variable "dlq_name" {
  type = string
}

variable "kms_key_arn" {
  type = string
}

variable "visibility_timeout_seconds" {
  description = "Must exceed the consumer's processing time; a stalled consumer's messages reappear after this."
  type        = number
  default     = 30
}

variable "max_receive_count" {
  type    = number
  default = 3
}
