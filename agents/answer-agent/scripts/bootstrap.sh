#!/usr/bin/env bash
# =============================================================================
# bootstrap.sh — First-time infrastructure setup.
#
# Run this ONCE before the first deploy.sh. It creates ECR, IAM, and S3
# without creating the AgentCore runtime (which requires the image to exist
# in ECR first).
#
# After bootstrap, the sequence is:
#   1. ./scripts/bootstrap.sh         (once)
#   2. Upload pageindex.json + questionnaire.md to S3
#   3. ./scripts/deploy.sh <tag>      (creates runtime + pushes image)
#   4. ./scripts/invoke.py ...        (test the deployment)
#
# Usage:
#   ./scripts/bootstrap.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
INFRA_DIR="${INFRA_DIR:-${ROOT_DIR}/infra}"

log() { echo "[bootstrap] $*"; }
die() { echo "[bootstrap] ERROR: $*" >&2; exit 1; }

[[ -f "${INFRA_DIR}/versions.tf" ]] || die "Infra directory not found at ${INFRA_DIR}"

cd "${INFRA_DIR}"

# --------------------------------------------------------------------------
# Ensure tfvars exists
# --------------------------------------------------------------------------

if [[ ! -f "terraform.tfvars" ]]; then
    if [[ -f "terraform.tfvars.example" ]]; then
        cp terraform.tfvars.example terraform.tfvars
        echo ""
        echo "  Created terraform.tfvars from example."
        echo "  EDIT ${INFRA_DIR}/terraform.tfvars before continuing."
        echo "  At minimum set: aws_region, project_name, environment"
        echo ""
        read -r -p "  Press Enter once you have edited terraform.tfvars, or Ctrl-C to abort..."
    else
        die "No terraform.tfvars found. Create it from terraform.tfvars.example."
    fi
fi

# --------------------------------------------------------------------------
# terraform init
# --------------------------------------------------------------------------

log "Running terraform init..."
terraform init -upgrade

# --------------------------------------------------------------------------
# Targeted apply: ECR + IAM + S3 only
# (runtime is NOT created here — it needs the image in ECR first)
# --------------------------------------------------------------------------

log "Creating ECR repository, IAM role, and S3 input bucket..."

# If the caller has set existing_role_arn in terraform.tfvars, skip the IAM
# targets — the role already exists and Terraform won't create it anyway
# (count = 0), but being explicit avoids a confusing "nothing to do" message.
EXISTING_ROLE="$(grep -E '^\s*existing_role_arn\s*=' terraform.tfvars 2>/dev/null | awk -F'"' '{print $2}' || true)"

IAM_TARGETS=""
if [[ -z "${EXISTING_ROLE}" ]]; then
    IAM_TARGETS=" \
        -target=aws_iam_role.runtime \
        -target=aws_iam_role_policy.ecr_pull \
        -target=aws_iam_role_policy.bedrock_invoke \
        -target=aws_iam_role_policy.s3_read \
        -target=aws_iam_role_policy.observability"
    log "  No existing_role_arn set — will create a new IAM role."
else
    log "  existing_role_arn is set (${EXISTING_ROLE}) — skipping IAM resource creation."
fi

terraform apply -auto-approve \
    -target=aws_ecr_repository.agent \
    -target=aws_ecr_lifecycle_policy.agent \
    ${IAM_TARGETS} \
    -target=aws_s3_bucket.input \
    -target=aws_s3_bucket_public_access_block.input \
    -target=aws_s3_bucket_versioning.input \
    -target=aws_s3_bucket_server_side_encryption_configuration.input

# --------------------------------------------------------------------------
# Print next steps
# --------------------------------------------------------------------------

ECR_URL="$(terraform output -raw ecr_repository_url)"
BUCKET="$(terraform output -raw input_bucket 2>/dev/null || echo '<bucket>')"
REGION="$(terraform output -raw region 2>/dev/null || echo 'us-east-1')"

echo ""
echo "========================================================"
echo "  Bootstrap complete."
echo ""
echo "  ECR repo : ${ECR_URL}"
echo "  S3 bucket: ${BUCKET}"
echo "  Region   : ${REGION}"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Upload your assets to S3:"
echo "     aws s3 cp /path/to/pageindex.json   s3://${BUCKET}/company/pageindex.json"
echo "     aws s3 cp /path/to/questionnaire.md s3://${BUCKET}/questionnaires/water.md"
echo ""
echo "  2. Update scripts/sample_payload.json with the correct s3:// URIs."
echo "     (replace YOUR_INPUT_BUCKET with: ${BUCKET})"
echo ""
echo "  3. Deploy:"
echo "     ./scripts/deploy.sh v1"
echo "========================================================"
