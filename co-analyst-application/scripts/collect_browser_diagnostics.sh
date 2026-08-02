#!/usr/bin/env bash
# Collect read-only diagnostics for one CoAnalyst download run.
#
# Usage:
#   AWS_PROFILE=my-profile ./collect_browser_diagnostics.sh RUN_ID [QUERY_ID]
#
# Optional:
#   AWS_REGION=us-east-1 LOG_SINCE=8h ./collect_browser_diagnostics.sh RUN_ID

set -u

RUN_ID="${1:-}"
QUERY_ID="${2:-}"
REGION="${AWS_REGION:-us-east-1}"
SINCE="${LOG_SINCE:-8h}"

if [[ -z "$RUN_ID" ]]; then
  echo "Usage: AWS_PROFILE=my-profile $0 RUN_ID [QUERY_ID]" >&2
  exit 2
fi

for command in aws jq tar; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Required command not found: $command" >&2
    exit 2
  fi
done

if ! aws sts get-caller-identity --region "$REGION" >/dev/null 2>&1; then
  echo "AWS authentication failed. Run your normal AWS SSO/login command first." >&2
  exit 2
fi

OUT_DIR="$(mktemp -d /tmp/csa-browser-diagnostics.XXXXXX)"
echo "Collecting diagnostics in $OUT_DIR"

capture() {
  local filename="$1"
  shift
  "$@" >"$OUT_DIR/$filename" 2>&1 || true
}

capture identity.json aws sts get-caller-identity \
  --region "$REGION" --output json

capture run-record.json aws dynamodb get-item \
  --table-name reportiq-runs \
  --key "{\"run_id\":{\"S\":\"$RUN_ID\"}}" \
  --consistent-read --region "$REGION" --output json

if [[ -z "$QUERY_ID" ]]; then
  QUERY_ID="$(jq -r '.Item.query_id.S // empty' "$OUT_DIR/run-record.json" 2>/dev/null)"
fi

if [[ -n "$QUERY_ID" ]]; then
  capture query-record.json aws dynamodb get-item \
    --table-name reportiq-web-queries \
    --key "{\"query_id\":{\"S\":\"$QUERY_ID\"}}" \
    --consistent-read --region "$REGION" --output json
else
  echo "No query_id was supplied or found in the run row." \
    >"$OUT_DIR/query-record.txt"
fi

capture browser-jobs.json aws dynamodb scan \
  --table-name reportiq-browser-jobs \
  --filter-expression "#run = :run" \
  --expression-attribute-names '{"#run":"run_id"}' \
  --expression-attribute-values "{\":run\":{\"S\":\"$RUN_ID\"}}" \
  --max-items 500 --region "$REGION" --output json

capture ecs-services.json aws ecs describe-services \
  --cluster reportiq-cluster \
  --services reportiq reportiq-browser-worker \
  --region "$REGION" --output json

for service in reportiq reportiq-browser-worker; do
  running_tasks="$(aws ecs list-tasks \
    --cluster reportiq-cluster --service-name "$service" \
    --desired-status RUNNING --region "$REGION" \
    --query 'taskArns[]' --output text 2>/dev/null)"
  if [[ -n "$running_tasks" && "$running_tasks" != "None" ]]; then
    # Intentional word splitting: AWS returns a tab-separated task ARN list.
    # shellcheck disable=SC2086
    capture "${service}-running-tasks.json" aws ecs describe-tasks \
      --cluster reportiq-cluster --tasks $running_tasks \
      --include TAGS --region "$REGION" --output json
  fi

  stopped_tasks="$(aws ecs list-tasks \
    --cluster reportiq-cluster --service-name "$service" \
    --desired-status STOPPED --max-items 20 --region "$REGION" \
    --query 'taskArns[]' --output text 2>/dev/null)"
  if [[ -n "$stopped_tasks" && "$stopped_tasks" != "None" ]]; then
    # shellcheck disable=SC2086
    capture "${service}-stopped-tasks.json" aws ecs describe-tasks \
      --cluster reportiq-cluster --tasks $stopped_tasks \
      --include TAGS --region "$REGION" --output json
  fi
done

for service in reportiq reportiq-browser-worker; do
  task_definition="$(jq -r \
    ".services[] | select(.serviceName == \"$service\") | .taskDefinition // empty" \
    "$OUT_DIR/ecs-services.json" 2>/dev/null | head -1)"
  if [[ -n "$task_definition" ]]; then
    capture "${service}-task-definition.json" aws ecs describe-task-definition \
      --task-definition "$task_definition" --include TAGS \
      --region "$REGION" --output json
  fi
done

for queue in reportiq-browser-jobs reportiq-browser-jobs-dlq; do
  queue_url="$(aws sqs get-queue-url --queue-name "$queue" \
    --region "$REGION" --query QueueUrl --output text 2>/dev/null)"
  if [[ -n "$queue_url" && "$queue_url" != "None" ]]; then
    capture "${queue}.json" aws sqs get-queue-attributes \
      --queue-url "$queue_url" --attribute-names All \
      --region "$REGION" --output json
  fi
done

capture fortis-s3-objects.json aws s3api list-objects-v2 \
  --bucket edo-coanalyst-report-610639371721 \
  --prefix fortis-healthcare-limited/ --max-items 300 \
  --region "$REGION" --output json

capture reportiq-app.log aws logs tail /ecs/reportiq \
  --since "$SINCE" --region "$REGION" --format short

capture browser-worker.log aws logs tail /ecs/reportiq-browser-worker \
  --since "$SINCE" --region "$REGION" --format short

capture agentcore-log-groups.json aws logs describe-log-groups \
  --log-group-name-prefix /aws/bedrock-agentcore/ \
  --region "$REGION" --output json

agent_groups="$(jq -r \
  '.logGroups[].logGroupName | select(test("edo.*coanalyst.*report"; "i"))' \
  "$OUT_DIR/agentcore-log-groups.json" 2>/dev/null)"
while IFS= read -r group; do
  [[ -z "$group" ]] && continue
  safe_name="$(printf '%s' "$group" | tr '/:' '__')"
  capture "agentcore-${safe_name}.log" aws logs tail "$group" \
    --since "$SINCE" --region "$REGION" --format short
done <<<"$agent_groups"

ARCHIVE="${OUT_DIR}.tar.gz"
tar -czf "$ARCHIVE" -C "$(dirname "$OUT_DIR")" "$(basename "$OUT_DIR")"

echo
echo "Diagnostics complete."
echo "Run ID: $RUN_ID"
echo "Query ID: ${QUERY_ID:-not found}"
echo "Archive: $ARCHIVE"
