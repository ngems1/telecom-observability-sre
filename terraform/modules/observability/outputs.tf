output "sns_topic_arn" {
  value = aws_sns_topic.alarms.arn
}

output "application_log_group" {
  value = aws_cloudwatch_log_group.container_insights["application"].name
}

output "dashboard_name" {
  value = aws_cloudwatch_dashboard.aws.dashboard_name
}
