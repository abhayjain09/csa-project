#!/usr/bin/env bash
# =============================================================================
# deploy.sh — One-command deploy for the PageIndex ReAct agent.
#
# Steps:
#   1. Build the ARM64 container image and push it to ECR.
#   2. Run terraform apply with the new image tag so the AgentCore runtime
#      picks up the new image.
#   3. Taint null_resource.runtime_update so Terraform registers a new
#      AgentCore runtime version even if the tag string didn't change.
#   4. Confirm the default endpoint is live and show the invoke command.
#
# Usage:
#   ./scripts/deploy.sh <tag>
#   e.g. ./scripts/deploy.sh v21
#        ./scripts/deploy.sh $(git rev-parse --short HEAD)   # recommended in CI
#
# Requires:
#   - terraform initialised in ./infra  (terraform init already run)
#   - AWS credentials with ECR push + bedrock-agentcore-control permissions
#   - docker buildx with ARM64 support
#     (on x86: docker run --privileged --rm tonistiigi/binfmt --install arm64)
#
# Environment overrides (optional):
#   INFRA_DIR   path to the terraform directory  (default: ./infra)
#   AGENT_DIR   path to the docker build context (default: ./agent)
#   AWS_REGION  overrides terraform output        (default: us-east-1)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
INFRA_DIR="${INFRA_DIR:-${ROOT_DIR}/infra}"
AGENT_DIR="${AGENT_DIR:-${ROOT_DIR}/agent}"

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

log()  { echo "[deploy] $*"; }
step() { echo ""; echo "==> $*"; }
die()  { echo "[deploy] ERROR: $*" >&2; exit 1; }

check_deps() {
    local missing=()
    for cmd in terraform aws docker git; do
        command -v "$cmd" &>/dev/null || missing+=("$cmd")
    done
    [[ ${#missing[@]} -eq 0 ]] || die "Missing required tools: ${missing[*]}"
}

# --------------------------------------------------------------------------
# Args
# --------------------------------------------------------------------------

TAG="${1:?Usage: ./scripts/deploy.sh <tag>   e.g. v21 or \$(git rev-parse --short HEAD)}"

# --------------------------------------------------------------------------
# Pre-flight
# --------------------------------------------------------------------------

check_deps

[[ -f "${INFRA_DIR}/versions.tf" ]]   || die "Terraform directory not found at ${INFRA_DIR}"
[[ -f "${AGENT_DIR}/Dockerfile" ]]    || die "Dockerfile not found at ${AGENT_DIR}"

cd "${INFRA_DIR}"

log "Resolving AWS context..."
REGION="$(terraform output -raw region 2>/dev/null || echo "${AWS_REGION:-us-east-1}")"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REPO="$(terraform output -raw ecr_repository_url 2>/dev/null)" \
    || die "Could not read ecr_repository_url from terraform output. Run: terraform init && terraform apply -target=aws_ecr_repository.agent"

log "Account : ${ACCOUNT}"
log "Region  : ${REGION}"
log "Repo    : ${REPO}"
log "Tag     : ${TAG}"

# --------------------------------------------------------------------------
# Step 1 — Build and push the ARM64 image
# --------------------------------------------------------------------------

step "1/4  Build + push  ${REPO}:${TAG}"
"${SCRIPT_DIR}/build_and_push.sh" "${ACCOUNT}" "${REGION}" "${REPO}" "${TAG}"

# --------------------------------------------------------------------------
# Step 2 — Taint null_resource.runtime_update so Terraform always registers
#          a new runtime version (the provider doesn't detect digest changes)
# --------------------------------------------------------------------------

step "2/4  Taint runtime_update trigger"
cd "${INFRA_DIR}"
terraform taint null_resource.runtime_update 2>/dev/null \
    || log "  (null_resource.runtime_update already tainted or not yet created — continuing)"

# --------------------------------------------------------------------------
# Step 3 — Terraform apply
# --------------------------------------------------------------------------

step "3/4  terraform apply  (image_tag=${TAG})"
terraform apply \
    -auto-approve \
    -var "image_tag=${TAG}"

# --------------------------------------------------------------------------
# Step 4 — Confirm endpoint advanced to the new version
# --------------------------------------------------------------------------

step "4/4  Confirm default endpoint"
RUNTIME_ID="$(terraform output -raw runtime_id)"
RUNTIME_ENDPOINT_ARN="$(terraform output -raw runtime_endpoint_arn)"

aws bedrock-agentcore-control list-agent-runtime-endpoints \
    --agent-runtime-id "${RUNTIME_ID}" \
    --region "${REGION}" \
    --query "runtimeEndpoints[].{name:name, live:liveVersion, status:status}" \
    --output table 2>/dev/null \
    || log "  (could not list endpoints — check AWS CLI version supports bedrock-agentcore-control)"

echo ""
echo "========================================================"
echo "  Deployed tag: ${TAG}"
echo "  Runtime ID  : ${RUNTIME_ID}"
echo "  Endpoint ARN: ${RUNTIME_ENDPOINT_ARN}"
echo ""
echo "  Invoke with:"
echo "    python3 scripts/invoke.py \\"
echo "      --endpoint-arn \"${RUNTIME_ENDPOINT_ARN}\" \\"
echo "      --payload scripts/sample_payload.json \\"
echo "      --region ${REGION}"
echo "========================================================"
