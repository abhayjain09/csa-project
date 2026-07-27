#!/usr/bin/env bash
# =============================================================================
# logs.sh — Tail the most recent CloudWatch log stream for the AgentCore runtime.
#
# Usage:
#   ./scripts/logs.sh                    # uses terraform output for runtime ID
#   ./scripts/logs.sh <runtime_id>       # supply runtime ID directly
#   ./scripts/logs.sh --follow           # keep tailing (like tail -f)
#
# Log group pattern:
#   /aws/bedrock-agentcore/runtimes/<runtime_id>-<endpoint_name>/runtime-logs
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
INFRA_DIR="${INFRA_DIR:-${ROOT_DIR}/infra}"

FOLLOW=false
RUNTIME_ID=""

for arg in "$@"; do
    case "$arg" in
        --follow|-f) FOLLOW=true ;;
        *) RUNTIME_ID="$arg" ;;
    esac
done

log() { echo "[logs] $*"; }
die() { echo "[logs] ERROR: $*" >&2; exit 1; }

# --------------------------------------------------------------------------
# Resolve runtime ID
# --------------------------------------------------------------------------

if [[ -z "${RUNTIME_ID}" ]]; then
    cd "${INFRA_DIR}"
    RUNTIME_ID="$(terraform output -raw runtime_id 2>/dev/null)" \
        || die "Could not read runtime_id from terraform output. Pass it as an argument."
    REGION="$(terraform output -raw region 2>/dev/null || echo "${AWS_REGION:-us-east-1}")"
else
    REGION="${AWS_REGION:-us-east-1}"
fi

LOG_GROUP_PREFIX="/aws/bedrock-agentcore/runtimes"
ENDPOINT_NAME="default"
LOG_GROUP="${LOG_GROUP_PREFIX}/${RUNTIME_ID}-${ENDPOINT_NAME}/runtime-logs"

log "Runtime ID : ${RUNTIME_ID}"
log "Log group  : ${LOG_GROUP}"
log "Region     : ${REGION}"

# --------------------------------------------------------------------------
# Find the most recent log stream
# --------------------------------------------------------------------------

STREAM="$(aws logs describe-log-streams \
    --log-group-name "${LOG_GROUP}" \
    --order-by LastEventTime \
    --descending \
    --max-items 1 \
    --region "${REGION}" \
    --query "logStreams[0].logStreamName" \
    --output text 2>/dev/null)" \
    || die "Could not list log streams for ${LOG_GROUP}. Has the runtime been invoked yet?"

if [[ "${STREAM}" == "None" || -z "${STREAM}" ]]; then
    die "No log streams found yet. Invoke the agent first."
fi

log "Stream     : ${STREAM}"
echo ""

# --------------------------------------------------------------------------
# Tail the stream
# --------------------------------------------------------------------------

if [[ "${FOLLOW}" == "true" ]]; then
    log "Following log stream (Ctrl-C to stop)..."
    aws logs tail "${LOG_GROUP}" \
        --log-stream-name-prefix "${STREAM}" \
        --follow \
        --region "${REGION}"
else
    aws logs get-log-events \
        --log-group-name "${LOG_GROUP}" \
        --log-stream-name "${STREAM}" \
        --start-from-head \
        --region "${REGION}" \
        --query "events[].message" \
        --output text
fi
