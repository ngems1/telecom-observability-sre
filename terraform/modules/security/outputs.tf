output "cloudtrail_bucket" {
  value = var.enable_cloudtrail ? aws_s3_bucket.trail[0].id : null
}

output "cloudtrail_log_group" {
  value = var.enable_cloudtrail ? aws_cloudwatch_log_group.trail[0].name : null
}
