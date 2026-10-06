variable "name" {
  description = "Cluster name."
  type        = string
}

variable "kubernetes_version" {
  type = string
}

variable "subnet_ids" {
  description = "Private subnets for the control-plane ENIs and the nodes."
  type        = list(string)
}

variable "kms_key_arn" {
  description = "Key for Kubernetes secret encryption and the control-plane log group."
  type        = string
}

variable "log_retention_days" {
  type = number
}

variable "public_access_cidrs" {
  type = list(string)
}

variable "admin_principal_arns" {
  type    = list(string)
  default = []
}

variable "node_instance_types" {
  type = list(string)
}

variable "node_capacity_type" {
  type = string
}

variable "node_min_size" {
  type = number
}

variable "node_desired_size" {
  type = number
}

variable "node_max_size" {
  type = number
}

variable "node_disk_gb" {
  type = number
}
