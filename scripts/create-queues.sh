#!/usr/bin/env bash
# Create the alert queue + dead-letter queue in a real AWS account (dev bootstrap).
# Terraform should own these in the real project; this script is the quick path.
#
#   AWS_REGION=us-east-1 ENVIRONMENT=dev ./scripts/create-queues.sh
#
# Prints the queue URL to put in the Helm value  usageApi.config.sqsQueueUrl
# (and notificationService.config.sqsQueueUrl).
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
QUEUE="usage-alerts-${ENVIRONMENT}"
DLQ="usage-alerts-dlq-${ENVIRONMENT}"
MAX_RECEIVES="${MAX_RECEIVES:-3}"

aws sqs create-queue --region "$REGION" --queue-name "$DLQ" \
  --attributes MessageRetentionPeriod=1209600 >/dev/null
DLQ_URL=$(aws sqs get-queue-url --region "$REGION" --queue-name "$DLQ" --query QueueUrl --output text)
DLQ_ARN=$(aws sqs get-queue-attributes --region "$REGION" --queue-url "$DLQ_URL" \
  --attribute-names QueueArn --query Attributes.QueueArn --output text)

ATTRS=$(python3 - "$DLQ_ARN" "$MAX_RECEIVES" <<'PY'
import json, sys
arn, max_receives = sys.argv[1], sys.argv[2]
print(json.dumps({
    "VisibilityTimeout": "30",
    "ReceiveMessageWaitTimeSeconds": "10",
    "MessageRetentionPeriod": "345600",
    "RedrivePolicy": json.dumps({"deadLetterTargetArn": arn, "maxReceiveCount": max_receives}),
}))
PY
)
aws sqs create-queue --region "$REGION" --queue-name "$QUEUE" --attributes "$ATTRS" >/dev/null

echo "Queue URL: $(aws sqs get-queue-url --region "$REGION" --queue-name "$QUEUE" --query QueueUrl --output text)"
echo "DLQ URL:   $DLQ_URL"
