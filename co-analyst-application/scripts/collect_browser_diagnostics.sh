#!/usr/bin/env bash
# Collect read-only diagnostics for recent CoAnalyst download runs or one ID.
#
# Usage (default AWS credential chain, every run from the last 2.5 hours):
#   ./collect_browser_diagnostics.sh
#
# Optional:
#   AWS_PROFILE=my-profile LOOKBACK_HOURS=4 ./collect_browser_diagnostics.sh
#   ./collect_browser_diagnostics.sh RUN_ID [QUERY_ID]

set -u

RUN_ID="${1:-}"
QUERY_ID="${2:-}"
REGION="${AWS_REGION:-us-east-1}"
LOOKBACK_HOURS="${LOOKBACK_HOURS:-2.5}"
SINCE="${LOG_SINCE:-${LOOKBACK_HOURS}h}"
RUNS_TABLE="${RUNS_TABLE:-reportiq-runs}"
QUERIES_TABLE="${QUERIES_TABLE:-reportiq-web-queries}"
BROWSER_JOBS_TABLE="${BROWSER_JOBS_TABLE:-reportiq-browser-jobs}"
PROVENANCE_TABLE="${PROVENANCE_TABLE:-edo-coanalyst-report-provenance}"
ECS_CLUSTER="${ECS_CLUSTER:-reportiq-cluster}"
APP_SERVICE="${APP_SERVICE:-reportiq}"
BROWSER_SERVICE="${BROWSER_SERVICE:-reportiq-browser-worker}"
APP_LOG_GROUP="${APP_LOG_GROUP:-/ecs/reportiq}"
BROWSER_LOG_GROUP="${BROWSER_LOG_GROUP:-/ecs/reportiq-browser-worker}"
DOWNLOAD_AGENT_LOG_GROUP="${DOWNLOAD_AGENT_LOG_GROUP:-/aws/bedrock-agentcore/edo-coanalyst-report}"
VERTEX_FUNCTION_NAME="${VERTEX_FUNCTION_NAME:-edo-coanalyst-report-vertex-search}"
VERTEX_LOG_GROUP="${VERTEX_LOG_GROUP:-/aws/lambda/${VERTEX_FUNCTION_NAME}}"
BROWSER_QUEUE_NAME="${BROWSER_QUEUE_NAME:-reportiq-browser-jobs}"
BROWSER_DLQ_NAME="${BROWSER_DLQ_NAME:-reportiq-browser-jobs-dlq}"
OUTPUT_DIR="${OUTPUT_DIR:-$PWD}"

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

SUMMARY_FILE="$OUT_DIR/run-summary.tsv"
BROWSER_SUMMARY_FILE="$OUT_DIR/browser-job-summary.tsv"
printf 'company\trun_id\tquery_id\tbulk_batch_id\tstatus\tdownloaded\tfailures\tbrowser_jobs\tqueued_at\tstarted_at\tupdated_at\terror\n' \
  >"$SUMMARY_FILE"
printf 'company\trun_id\tjob_id\tdocument\tstatus\tattempts\tcreated_at\tupdated_at\terror\n' \
  >"$BROWSER_SUMMARY_FILE"

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

  company="$(jq -r '(.Item.company.S // "") | gsub("[\\t\\r\\n]"; " ")' \
    "$OUT_DIR/$run_file" 2>/dev/null)"
  summary_query_id="$(jq -r '.Item.query_id.S // ""' \
    "$OUT_DIR/$run_file" 2>/dev/null)"
  bulk_batch_id="$(jq -r '.Item.bulk_batch_id.S // ""' \
    "$OUT_DIR/$run_file" 2>/dev/null)"
  run_status="$(jq -r '.Item.status.S // ""' "$OUT_DIR/$run_file" 2>/dev/null)"
  downloaded_count="$(jq -r \
    '(.Item.downloaded.S // "[]" | fromjson? // []) | length' \
    "$OUT_DIR/$run_file" 2>/dev/null)"
  failure_count="$(jq -r \
    '(.Item.failures.S // "[]" | fromjson? // []) | length' \
    "$OUT_DIR/$run_file" 2>/dev/null)"
  browser_statuses="$(jq -r \
    '[.Items[]?.status.S // "unknown"] | sort | group_by(.) | map("\(.[0])=\(length)") | join(",")' \
    "$OUT_DIR/browser-jobs-${safe_run_id}.json" 2>/dev/null)"
  queued_at="$(jq -r '.Item.queued_at.S // ""' "$OUT_DIR/$run_file" 2>/dev/null)"
  started_at="$(jq -r '.Item.started_at.S // ""' "$OUT_DIR/$run_file" 2>/dev/null)"
  updated_at="$(jq -r '.Item.updated_at.S // ""' "$OUT_DIR/$run_file" 2>/dev/null)"
  run_error="$(jq -r '(.Item.error_msg.S // .Item.error.S // "") | gsub("[\\t\\r\\n]"; " ")' \
    "$OUT_DIR/$run_file" 2>/dev/null)"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$company" "$current_run_id" "$summary_query_id" "$bulk_batch_id" \
    "$run_status" "${downloaded_count:-0}" "${failure_count:-0}" \
    "$browser_statuses" "$queued_at" "$started_at" "$updated_at" "$run_error" \
    >>"$SUMMARY_FILE"

  jq -r --arg company "$company" --arg run "$current_run_id" '
    .Items[]? | [
      $company,
      $run,
      (.job_id.S // ""),
      (.document_name.S // .report_class.S // .query.S // ""),
      (.status.S // ""),
      (.attempts.N // .attempt_count.N // ""),
      (.created_at.S // .queued_at.S // ""),
      (.updated_at.S // ""),
      ((.error_msg.S // .error.S // "") | gsub("[\\t\\r\\n]"; " "))
    ] | @tsv' "$OUT_DIR/browser-jobs-${safe_run_id}.json" \
    >>"$BROWSER_SUMMARY_FILE" 2>/dev/null || true
done

capture ecs-services.json aws ecs describe-services \
  --cluster "$ECS_CLUSTER" \
  --services "$APP_SERVICE" "$BROWSER_SERVICE" \
  --region "$REGION" --output json

for service in "$APP_SERVICE" "$BROWSER_SERVICE"; do
  running_tasks="$(aws ecs list-tasks \
    --cluster "$ECS_CLUSTER" --service-name "$service" \
    --desired-status RUNNING --region "$REGION" \
    --query 'taskArns[]' --output text 2>/dev/null)"
  if [[ -n "$running_tasks" && "$running_tasks" != "None" ]]; then
    # Intentional word splitting: AWS returns a tab-separated task ARN list.
    # shellcheck disable=SC2086
    capture "${service}-running-tasks.json" aws ecs describe-tasks \
      --cluster "$ECS_CLUSTER" --tasks $running_tasks \
      --include TAGS --region "$REGION" --output json
  fi

  stopped_tasks="$(aws ecs list-tasks \
    --cluster "$ECS_CLUSTER" --service-name "$service" \
    --desired-status STOPPED --max-items 20 --region "$REGION" \
    --query 'taskArns[]' --output text 2>/dev/null)"
  if [[ -n "$stopped_tasks" && "$stopped_tasks" != "None" ]]; then
    # shellcheck disable=SC2086
    capture "${service}-stopped-tasks.json" aws ecs describe-tasks \
      --cluster "$ECS_CLUSTER" --tasks $stopped_tasks \
      --include TAGS --region "$REGION" --output json
  fi
done

for service in "$APP_SERVICE" "$BROWSER_SERVICE"; do
  task_definition="$(jq -r \
    ".services[] | select(.serviceName == \"$service\") | .taskDefinition // empty" \
    "$OUT_DIR/ecs-services.json" 2>/dev/null | head -1)"
  if [[ -n "$task_definition" ]]; then
    capture "${service}-task-definition.json" aws ecs describe-task-definition \
      --task-definition "$task_definition" --include TAGS \
      --region "$REGION" --output json
  fi
done

# Capture the effective ECS roles and inline/attached policies so deployment
# regressions such as a missing browser-state permission are visible in the
# same archive as the runtime failure.
for service in "$APP_SERVICE" "$BROWSER_SERVICE"; do
  task_file="$OUT_DIR/${service}-task-definition.json"
  [[ -f "$task_file" ]] || continue
  for role_field in taskRoleArn executionRoleArn; do
    role_arn="$(jq -r ".taskDefinition.${role_field} // empty" \
      "$task_file" 2>/dev/null)"
    [[ -n "$role_arn" ]] || continue
    role_name="${role_arn##*/}"
    safe_role="$(printf '%s' "$role_name" | tr -cd 'A-Za-z0-9+=,.@_-')"
    capture "iam-role-${safe_role}.json" aws iam get-role \
      --role-name "$role_name" --output json
    capture "iam-attached-${safe_role}.json" aws iam list-attached-role-policies \
      --role-name "$role_name" --output json
    capture "iam-inline-${safe_role}.json" aws iam list-role-policies \
      --role-name "$role_name" --output json
    inline_policies="$(jq -r '.PolicyNames[]? // empty' \
      "$OUT_DIR/iam-inline-${safe_role}.json" 2>/dev/null)"
    while IFS= read -r policy_name; do
      [[ -n "$policy_name" ]] || continue
      safe_policy="$(printf '%s' "$policy_name" | tr -cd 'A-Za-z0-9+=,.@_-')"
      capture "iam-policy-${safe_role}-${safe_policy}.json" aws iam get-role-policy \
        --role-name "$role_name" --policy-name "$policy_name" --output json
    done <<<"$inline_policies"
  done
done

worker_task_file="$OUT_DIR/${BROWSER_SERVICE}-task-definition.json"
if [[ -f "$worker_task_file" ]]; then
  browser_state_bucket="$(jq -r '
    .taskDefinition.containerDefinitions[]?.environment[]?
    | select(.name == "BROWSER_STATE_BUCKET") | .value' \
    "$worker_task_file" 2>/dev/null | head -1)"
  browser_state_prefix="$(jq -r '
    .taskDefinition.containerDefinitions[]?.environment[]?
    | select(.name == "BROWSER_WORKER_STATE_PREFIX") | .value' \
    "$worker_task_file" 2>/dev/null | head -1)"
  if [[ -n "$browser_state_bucket" ]]; then
    capture browser-state-objects.json aws s3api list-objects-v2 \
      --bucket "$browser_state_bucket" \
      --prefix "${browser_state_prefix:-_browser-state}/" \
      --max-keys 100 --region "$REGION" --output json
  fi
fi

for queue in "$BROWSER_QUEUE_NAME" "$BROWSER_DLQ_NAME"; do
  queue_url="$(aws sqs get-queue-url --queue-name "$queue" \
    --region "$REGION" --query QueueUrl --output text 2>/dev/null)"
  if [[ -n "$queue_url" && "$queue_url" != "None" ]]; then
    capture "${queue}.json" aws sqs get-queue-attributes \
      --queue-url "$queue_url" --attribute-names All \
      --region "$REGION" --output json
  fi
done

capture reportiq-app.log aws logs tail "$APP_LOG_GROUP" \
  --since "$SINCE" --region "$REGION" --format short

capture browser-worker.log aws logs tail "$BROWSER_LOG_GROUP" \
  --since "$SINCE" --region "$REGION" --format short

capture vertex-search.log aws logs tail "$VERTEX_LOG_GROUP" \
  --since "$SINCE" --region "$REGION" --format short

capture vertex-function.json aws lambda get-function \
  --function-name "$VERTEX_FUNCTION_NAME" --region "$REGION" \
  --query '{Configuration:{FunctionName:Configuration.FunctionName,LastModified:Configuration.LastModified,State:Configuration.State,LastUpdateStatus:Configuration.LastUpdateStatus,PackageType:Configuration.PackageType,Architectures:Configuration.Architectures,MemorySize:Configuration.MemorySize,Timeout:Configuration.Timeout,RevisionId:Configuration.RevisionId},Code:{ImageUri:Code.ImageUri,ResolvedImageUri:Code.ResolvedImageUri}}' \
  --output json

# This command is intentionally best-effort: older AWS CLI installations may
# not yet expose the AgentCore control subcommands. The resulting error is still
# useful in the archive and never aborts collection.
capture agentcore-runtimes.json aws bedrock-agentcore-control list-agent-runtimes \
  --region "$REGION" --output json

capture agentcore-log-groups.json aws logs describe-log-groups \
  --log-group-name-prefix /aws/bedrock-agentcore/ \
  --region "$REGION" --output json

agent_groups="$(jq -r --arg configured "$DOWNLOAD_AGENT_LOG_GROUP" '
  [.logGroups[]?.logGroupName,
   $configured]
  | unique[]
  | select(test("coanalyst|reportiq|download.agent|edo.*report"; "i"))' \
  "$OUT_DIR/agentcore-log-groups.json" 2>/dev/null)"
while IFS= read -r group; do
  [[ -z "$group" ]] && continue
  safe_name="$(printf '%s' "$group" | tr '/:' '__')"
  capture "agentcore-${safe_name}.log" aws logs tail "$group" \
    --since "$SINCE" --region "$REGION" --format short
done <<<"$agent_groups"

printf '%s\n' "${RUN_IDS[@]}" >"$OUT_DIR/run-ids.txt"

ARCHIVE="${OUTPUT_DIR%/}/$(basename "$OUT_DIR").tar.gz"
tar -czf "$ARCHIVE" -C "$(dirname "$OUT_DIR")" "$(basename "$OUT_DIR")"

echo
echo "Diagnostics complete."
echo "Lookback: $LOOKBACK_HOURS hour(s), starting $CUTOFF_ISO"
echo "Run IDs found: ${#RUN_IDS[@]}"
company_count="$(tail -n +2 "$SUMMARY_FILE" | cut -f1 | sed '/^$/d' | sort -u | wc -l | tr -d ' ')"
echo "Companies found: ${company_count:-0}"
for current_run_id in "${RUN_IDS[@]}"; do
  echo "  $current_run_id"
done
echo
echo "Run summary:"
if command -v column >/dev/null 2>&1; then
  column -t -s $'\t' "$SUMMARY_FILE"
else
  cat "$SUMMARY_FILE"
fi
echo
echo "Browser job summary:"
if command -v column >/dev/null 2>&1; then
  column -t -s $'\t' "$BROWSER_SUMMARY_FILE"
else
  cat "$BROWSER_SUMMARY_FILE"
fi
echo "Archive: $ARCHIVE"
