terraform {
  # >= 1.10 for S3 native state locking (use_lockfile), so no DynamoDB table is needed.
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Partial configuration: bucket/key/region come from envs/backend-<env>.hcl at `terraform init`.
  backend "s3" {}
}
