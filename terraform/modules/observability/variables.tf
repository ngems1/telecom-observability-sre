variable "name" {
  type = string
}

variable "region" {
  type = string
}

variable "cluster_name" {
  type = string
}

variable "kms_key_arn" {
  type = string
}

variable "log_retention_days" {
  type = number
}

variable "alert_email" {
  type    = string
  default = ""
}

variable "queue_name" {
  type = string
}

variable "dlq_name" {
  type = string
}

variable "db_identifier" {
  type = string
}

variable "sqs_alarm_age_seconds" {
  type = number
}
