#!/usr/bin/env bash
# One-command deploy: terraform apply first (creates ECR repo), then
# build + push the ARM64 image, then terraform apply again to update
# the runtime with the new image tag.
#
# After apply, AGENTCORE_RUNTIME_ARN is exported automatically so you can
# run the indexer immediately without any manual env var setup.
#
# Usage: ./scripts/deploy.sh <tag>
#   e.g. ./scripts/deploy.sh v1
#        ./scripts/deploy.sh v2
#
# Prerequisites:
#   - cd infra && terraform init  (first time only)
#   - AWS credentials via EC2 instance profile (auto-resolved on EC2)
#   - docker buildx available
#   - pageindex-lib/ populated: git clone <repo> pageindex-lib
set -euo pipefail

TAG="${1:?usage: ./scripts/deploy.sh <tag>   (e.g. v1)}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${SCRIPT_DIR}/../infra"
TFVARS="${INFRA_DIR}/terraform.tfvars"

# ── Resolve region ────────────────────────────────────────────────────────────
# On EC2 the instance metadata endpoint is always available and is the most
# reliable source — no dependency on terraform outputs existing yet.
if curl -sf --connect-timeout 2 http://169.254.169.254/latest/meta-data/placement/region > /tmp/_region 2>/dev/null; then
  REGION="$(cat /tmp/_region)"
  echo "  [deploy] region resolved from EC2 instance metadata: ${REGION}"
elif REGION="$(cd "$INFRA_DIR" && terraform output -raw aws_region 2>/dev/null)" && [ -n "$REGION" ]; then
  echo "  [deploy] region resolved from terraform output: ${REGION}"
elif REGION="$(grep 'aws_region' "$TFVARS" | awk -F'"' '{print $2}')" && [ -n "$REGION" ]; then
  echo "  [deploy] region resolved from terraform.tfvars: ${REGION}"
else
  REGION="${AWS_REGION:-us-east-1}"
  echo "  [deploy] region fallback: ${REGION}"
fi

# ── Resolve account ID ────────────────────────────────────────────────────────
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"

# ── Derive ECR repo URL from tfvars (before first apply) ─────────────────────
ECR_REPO_NAME="$(grep 'ecr_repo_name' "$TFVARS" | awk -F'"' '{print $2}')"
REPO="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO_NAME}"

echo ""
echo "======================================================"
echo " PageIndex AgentCore — deploy ${TAG}"
echo " account : ${ACCOUNT}"
echo " region  : ${REGION}"
echo " repo    : ${REPO}"
echo "======================================================"

# ── Step 1: terraform apply (ECR repo + IAM role only) ───────────────────────
# Target only ECR and IAM — do NOT create the runtime yet because the image
# doesn't exist in ECR until step 2. The runtime requires the image to exist.
echo ""
echo "==> 1/4  terraform apply  (ECR repo + IAM role only)"
cd "$INFRA_DIR"
terraform apply -auto-approve   -target=aws_ecr_repository.pageindex   -target=aws_ecr_lifecycle_policy.pageindex   -target=aws_iam_role.pageindex   -target=aws_iam_role_policy.pageindex   -var "image_tag=${TAG}"

# Refresh ECR URL from terraform output now that it exists
REPO="$(terraform output -raw ecr_repository_url)"

# ── Step 2: build + push image ───────────────────────────────────────────────
echo ""
echo "==> 2/4  build + push  ${REPO}:${TAG}"
"${SCRIPT_DIR}/build_and_push.sh" "$ACCOUNT" "$REGION" "$REPO" "$TAG"

# ── Step 3: terraform apply again — updates runtime to use new image tag ─────
# AgentCore does not auto-detect image changes so we re-apply to force a
# new runtime version with the freshly pushed image.
echo ""
echo "==> 3/4  terraform apply  (update runtime to image_tag=${TAG})"
terraform apply -auto-approve -var "image_tag=${TAG}"

# ── Step 4: export runtime ARN + verify endpoint ─────────────────────────────
echo ""
echo "==> 4/4  verify runtime endpoint"
RUNTIME_ARN="$(terraform output -raw runtime_arn)"
RUNTIME_ID="${RUNTIME_ARN##*/}"

aws bedrock-agentcore-control list-agent-runtime-endpoints \
  --agent-runtime-id "$RUNTIME_ID" \
  --region "$REGION" \
  --query "runtimeEndpoints[].{name:name, live:liveVersion, status:status}" \
  --output table

# ── Export AGENTCORE_RUNTIME_ARN for immediate use ───────────────────────────
export AGENTCORE_RUNTIME_ARN="$RUNTIME_ARN"

echo ""
echo "======================================================"
echo " Deployed ${TAG} successfully."
echo ""
echo " AGENTCORE_RUNTIME_ARN has been exported for this session."
echo " To persist across sessions, add to ~/.bashrc:"
echo "   export AGENTCORE_RUNTIME_ARN=${RUNTIME_ARN}"
echo ""
echo " Run the indexer:"
echo "   python build_pdf_index.py --s3-prefix paccar/"
echo ""
echo " Smoke-test a single PDF:"
echo "   python scripts/invoke_local.py \\"
echo "     ${RUNTIME_ARN} \\"
echo "     scripts/payload.example.json \\"
echo "     --region=${REGION}"
echo "======================================================"

