#!/bin/bash
# Runs inside LocalStack when it is ready: creates the alert queue and its dead-letter queue.
# Local values are short so the DLQ demo is quick: 10s visibility x 3 receives = ~30s to the DLQ.
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
QUEUE="usage-alerts-dev"
DLQ="usage-alerts-dlq-dev"

awslocal sqs create-queue --region "$REGION" --queue-name "$DLQ" >/dev/null
DLQ_URL=$(awslocal sqs get-queue-url --region "$REGION" --queue-name "$DLQ" --query QueueUrl --output text)
DLQ_ARN=$(awslocal sqs get-queue-attributes --region "$REGION" --queue-url "$DLQ_URL" \
  --attribute-names QueueArn --query Attributes.QueueArn --output text)

cat > /tmp/queue-attrs.json <<JSON
{"VisibilityTimeout":"10","RedrivePolicy":"{\"deadLetterTargetArn\":\"${DLQ_ARN}\",\"maxReceiveCount\":\"3\"}"}
JSON
awslocal sqs create-queue --region "$REGION" --queue-name "$QUEUE" --attributes file:///tmp/queue-attrs.json >/dev/null

echo "queues ready: $QUEUE (dlq: $DLQ)"
