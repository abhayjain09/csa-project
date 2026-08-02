#!/usr/bin/env bash
# Collect read-only diagnostics for recent CoAnalyst download runs or one ID.
#
# Usage (default AWS profile, every run from the last two hours):
#   ./collect_browser_diagnostics.sh
#
# Optional:
#   AWS_PROFILE=my-profile LOOKBACK_HOURS=4 ./collect_browser_diagnostics.sh
#   ./collect_browser_diagnostics.sh RUN_ID [QUERY_ID]

set -u

RUN_ID="${1:-}"
QUERY_ID="${2:-}"
REGION="${AWS_REGION:-us-east-1}"
LOOKBACK_HOURS="${LOOKBACK_HOURS:-2}"
SINCE="${LOG_SINCE:-${LOOKBACK_HOURS}h}"
RUNS_TABLE="${RUNS_TABLE:-reportiq-runs}"
QUERIES_TABLE="${QUERIES_TABLE:-reportiq-web-queries}"
BROWSER_JOBS_TABLE="${BROWSER_JOBS_TABLE:-reportiq-browser-jobs}"
PROVENANCE_TABLE="${PROVENANCE_TABLE:-edo-coanalyst-report-provenance}"

for command in aws jq tar python3; do
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

CUTOFF_ISO="$(python3 - "$LOOKBACK_HOURS" <<'PY'
import sys
from datetime import datetime, timedelta, timezone

hours = float(sys.argv[1])
print((datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat())
PY
)"

RUN_IDS=()
if [[ -n "$RUN_ID" ]]; then
  RUN_IDS+=("$RUN_ID")
else
  capture recent-runs.json aws dynamodb scan \
    --table-name "$RUNS_TABLE" \
    --filter-expression "#started >= :cutoff OR #updated >= :cutoff OR #queued >= :cutoff" \
    --expression-attribute-names \
      '{"#started":"started_at","#updated":"updated_at","#queued":"queued_at"}' \
    --expression-attribute-values "{\":cutoff\":{\"S\":\"$CUTOFF_ISO\"}}" \
    --region "$REGION" --output json

  while IFS= read -r recent_run_id; do
    [[ -n "$recent_run_id" ]] && RUN_IDS+=("$recent_run_id")
  done < <(jq -r '.Items[]?.run_id.S // empty' "$OUT_DIR/recent-runs.json" 2>/dev/null)
fi

if [[ "${#RUN_IDS[@]}" -eq 0 ]]; then
  echo "No runs found since $CUTOFF_ISO" >"$OUT_DIR/no-recent-runs.txt"
fi

for current_run_id in "${RUN_IDS[@]}"; do
  safe_run_id="$(printf '%s' "$current_run_id" | tr -cd 'A-Za-z0-9._-')"
  run_file="run-${safe_run_id}.json"
  capture "$run_file" aws dynamodb get-item \
    --table-name "$RUNS_TABLE" \
    --key "{\"run_id\":{\"S\":\"$current_run_id\"}}" \
    --consistent-read --region "$REGION" --output json

  current_query_id="$QUERY_ID"
  if [[ -z "$current_query_id" ]]; then
    current_query_id="$(jq -r '.Item.query_id.S // empty' \
      "$OUT_DIR/$run_file" 2>/dev/null)"
  fi
  if [[ -n "$current_query_id" ]]; then
    capture "query-${safe_run_id}.json" aws dynamodb get-item \
      --table-name "$QUERIES_TABLE" \
      --key "{\"query_id\":{\"S\":\"$current_query_id\"}}" \
      --consistent-read --region "$REGION" --output json
  fi

  capture "browser-jobs-${safe_run_id}.json" aws dynamodb scan \
    --table-name "$BROWSER_JOBS_TABLE" \
    --filter-expression "#run = :run" \
    --expression-attribute-names '{"#run":"run_id"}' \
    --expression-attribute-values \
      "{\":run\":{\"S\":\"$current_run_id\"}}" \
    --region "$REGION" --output json

  capture "provenance-${safe_run_id}.json" aws dynamodb scan \
    --table-name "$PROVENANCE_TABLE" \
    --filter-expression "#run = :run" \
    --expression-attribute-names '{"#run":"run_id"}' \
    --expression-attribute-values \
      "{\":run\":{\"S\":\"$current_run_id\"}}" \
    --region "$REGION" --output json
done

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
echo "Lookback: $LOOKBACK_HOURS hour(s), starting $CUTOFF_ISO"
echo "Run IDs found: ${#RUN_IDS[@]}"
for current_run_id in "${RUN_IDS[@]}"; do
  echo "  $current_run_id"
done
echo "Archive: $ARCHIVE"
