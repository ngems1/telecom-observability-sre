# CloudWatch side of the observability stack: log groups with retention (cost control), an SNS topic,
# alarms for the AWS-managed parts of the pipeline (SQS, RDS, nodes) and one dashboard.
# Application SLO alerts (burn rates) live in Prometheus, see charts/telecom-app/files/rules.

locals {
  container_insights_groups = ["application", "dataplane", "host", "performance"]
}

# Container Insights / Fluent Bit write here. Pre-creating them applies retention and encryption;
# otherwise the agent creates them with "never expire", which silently grows the logging bill.
resource "aws_cloudwatch_log_group" "container_insights" {
  for_each = toset(local.container_insights_groups)

  name              = "/aws/containerinsights/${var.cluster_name}/${each.value}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

# ------------------------------------------------------------ notifications
resource "aws_sns_topic" "alarms" {
  name              = "${var.name}-alarms"
  kms_master_key_id = var.kms_key_arn
}

resource "aws_sns_topic_subscription" "email" {
  count = var.alert_email != "" ? 1 : 0

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ------------------------------------------------------------ SQS alarms
resource "aws_cloudwatch_metric_alarm" "queue_age" {
  alarm_name          = "${var.name}-sqs-oldest-message-age"
  alarm_description   = "Notifications are not being consumed: oldest message is older than ${var.sqs_alarm_age_seconds}s. Pods may look healthy. Runbook: docs/failure-scenarios.md#scenario-3---stuck-queue-processing"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = var.queue_name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = var.sqs_alarm_age_seconds
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  tags = { severity = "critical", owner = "notifications-team" }
}

resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  alarm_name          = "${var.name}-sqs-dlq-not-empty"
  alarm_description   = "A message exhausted its retries and landed in the dead-letter queue (poison message or persistent delivery failure). Runbook: docs/failure-scenarios.md#bonus-a---poison-message-and-the-dead-letter-queue"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = var.dlq_name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  tags = { severity = "warning", owner = "notifications-team" }
}

resource "aws_cloudwatch_metric_alarm" "queue_backlog" {
  alarm_name          = "${var.name}-sqs-backlog"
  alarm_description   = "More than 500 messages waiting: consumers are too slow for the producer rate."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = var.queue_name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  threshold           = 500
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  tags = { severity = "warning", owner = "notifications-team" }
}

# ------------------------------------------------------------ RDS alarms
resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "${var.name}-rds-cpu-high"
  alarm_description   = "Database CPU above 80% for 10 minutes."
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  dimensions          = { DBInstanceIdentifier = var.db_identifier }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "missing"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  tags = { severity = "warning", owner = "data-platform" }
}

resource "aws_cloudwatch_metric_alarm" "rds_storage" {
  alarm_name          = "${var.name}-rds-free-storage-low"
  alarm_description   = "Less than 2 GiB of free storage on the database."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  dimensions          = { DBInstanceIdentifier = var.db_identifier }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 2147483648
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "missing"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  tags = { severity = "critical", owner = "data-platform" }
}

resource "aws_cloudwatch_metric_alarm" "rds_connections" {
  alarm_name          = "${var.name}-rds-connections-high"
  alarm_description   = "More than 60 open database connections (small instance classes allow roughly 80-100)."
  namespace           = "AWS/RDS"
  metric_name         = "DatabaseConnections"
  dimensions          = { DBInstanceIdentifier = var.db_identifier }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 60
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "missing"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  tags = { severity = "warning", owner = "data-platform" }
}

# ------------------------------------------------------------ cluster alarm (Container Insights metric)
resource "aws_cloudwatch_metric_alarm" "failed_nodes" {
  alarm_name          = "${var.name}-eks-failed-nodes"
  alarm_description   = "At least one EKS worker node is NotReady."
  namespace           = "ContainerInsights"
  metric_name         = "cluster_failed_node_count"
  dimensions          = { ClusterName = var.cluster_name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  tags = { severity = "critical", owner = "platform-sre" }
}

# ------------------------------------------------------------ dashboard
resource "aws_cloudwatch_dashboard" "aws" {
  dashboard_name = "${var.name}-aws-services"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 2
        properties = {
          markdown = "# ${var.name}: AWS-managed layers\nSQS pipeline, RDS and EKS nodes. Application SLOs and per-route latency are in Grafana (Telecom folder)."
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 2
        width  = 8
        height = 6
        properties = {
          title  = "SQS: oldest message age (s)"
          region = var.region
          view   = "timeSeries"
          stat   = "Maximum"
          period = 60
          metrics = [
            ["AWS/SQS", "ApproximateAgeOfOldestMessage", "QueueName", var.queue_name],
          ]
          annotations = { horizontal = [{ label = "alarm", value = var.sqs_alarm_age_seconds }] }
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 2
        width  = 8
        height = 6
        properties = {
          title  = "SQS: messages waiting (queue and DLQ)"
          region = var.region
          view   = "timeSeries"
          stat   = "Maximum"
          period = 60
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", var.queue_name],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", var.dlq_name],
          ]
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 2
        width  = 8
        height = 6
        properties = {
          title  = "SQS: sent vs deleted (per minute)"
          region = var.region
          view   = "timeSeries"
          stat   = "Sum"
          period = 60
          metrics = [
            ["AWS/SQS", "NumberOfMessagesSent", "QueueName", var.queue_name],
            ["AWS/SQS", "NumberOfMessagesDeleted", "QueueName", var.queue_name],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 8
        width  = 8
        height = 6
        properties = {
          title  = "RDS: CPU %"
          region = var.region
          view   = "timeSeries"
          stat   = "Average"
          period = 60
          metrics = [
            ["AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", var.db_identifier],
          ]
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 8
        width  = 8
        height = 6
        properties = {
          title  = "RDS: connections and free memory"
          region = var.region
          view   = "timeSeries"
          period = 60
          metrics = [
            ["AWS/RDS", "DatabaseConnections", "DBInstanceIdentifier", var.db_identifier, { stat = "Maximum" }],
            ["AWS/RDS", "FreeableMemory", "DBInstanceIdentifier", var.db_identifier, { stat = "Minimum", yAxis = "right" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 8
        width  = 8
        height = 6
        properties = {
          title  = "RDS: read / write latency (s)"
          region = var.region
          view   = "timeSeries"
          stat   = "Average"
          period = 60
          metrics = [
            ["AWS/RDS", "ReadLatency", "DBInstanceIdentifier", var.db_identifier],
            ["AWS/RDS", "WriteLatency", "DBInstanceIdentifier", var.db_identifier],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 14
        width  = 12
        height = 6
        properties = {
          title  = "EKS nodes: CPU and memory utilization %"
          region = var.region
          view   = "timeSeries"
          stat   = "Average"
          period = 60
          metrics = [
            ["ContainerInsights", "node_cpu_utilization", "ClusterName", var.cluster_name],
            ["ContainerInsights", "node_memory_utilization", "ClusterName", var.cluster_name],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 14
        width  = 12
        height = 6
        properties = {
          title  = "EKS: nodes and failed nodes"
          region = var.region
          view   = "timeSeries"
          stat   = "Maximum"
          period = 60
          metrics = [
            ["ContainerInsights", "cluster_node_count", "ClusterName", var.cluster_name],
            ["ContainerInsights", "cluster_failed_node_count", "ClusterName", var.cluster_name],
          ]
        }
      },
    ]
  })
}

# ------------------------------------------------------------ saved Logs Insights queries (incident investigation)
# Container Insights' Fluent Bit merges each JSON log line under `log_processed`, so the app's fields
# (service, level, correlation_id, route, status, duration_ms, ...) are queried as log_processed.<field>.
locals {
  app_log_group = aws_cloudwatch_log_group.container_insights["application"].name

  saved_queries = {
    "01-follow-a-correlation-id" = <<-EOT
      # Replace the ID with the X-Correlation-ID returned by the API (or from an alert log line).
      fields @timestamp, log_processed.service, log_processed.level, log_processed.message, log_processed.route, log_processed.status, log_processed.event_id
      | filter log_processed.correlation_id = "REPLACE-WITH-CORRELATION-ID"
      | sort @timestamp asc
    EOT

    "02-errors-by-service" = <<-EOT
      fields @timestamp, log_processed.service, log_processed.message
      | filter log_processed.level in ["ERROR", "CRITICAL"]
      | stats count(*) as errors by log_processed.service, log_processed.message
      | sort errors desc
    EOT

    "03-slow-api-requests" = <<-EOT
      fields @timestamp, log_processed.route, log_processed.status, log_processed.duration_ms, log_processed.correlation_id
      | filter log_processed.service = "usage-api" and log_processed.message = "request" and log_processed.duration_ms > 300
      | stats count(*) as slow, pct(log_processed.duration_ms, 95) as p95_ms by log_processed.route
      | sort slow desc
    EOT

    "04-notification-outcomes" = <<-EOT
      fields @timestamp, log_processed.message
      | filter log_processed.service = "notification-service"
      | stats count(*) as events by log_processed.message, bin(5m)
    EOT

    "05-failure-injection-audit" = <<-EOT
      fields @timestamp, log_processed.service, log_processed.message, @message
      | filter log_processed.message like /chaos/
      | sort @timestamp desc
    EOT
  }
}

resource "aws_cloudwatch_query_definition" "saved" {
  for_each = local.saved_queries

  name            = "${var.name}/${each.key}"
  log_group_names = [local.app_log_group]
  query_string    = each.value
}
