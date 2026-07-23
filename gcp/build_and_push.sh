#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# --- Configuration ---
AWS_ACCOUNT_ID="610639371721"
AWS_REGION="us-east-1"
ECR_REPO_NAME="gcp-vertex-lambda"
IMAGE_TAG="latest"
# ---------------------

ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
FULL_IMAGE_URI="${ECR_URI}/${ECR_REPO_NAME}:${IMAGE_TAG}"

echo "=== Step 1: Authenticating Docker with ECR ==="
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${ECR_URI}"

echo ""
echo "=== Step 2: Creating ECR repository (skips if already exists) ==="
aws ecr describe-repositories --repository-names "${ECR_REPO_NAME}" \
    --region "${AWS_REGION}" > /dev/null 2>&1 \
  || aws ecr create-repository \
        --repository-name "${ECR_REPO_NAME}" \
        --region "${AWS_REGION}" \
        --image-scanning-configuration scanOnPush=true \
        --encryption-configuration encryptionType=AES256

echo ""
echo "=== Step 3: Building Docker image ==="
docker build --platform linux/amd64 \
  -t "${ECR_REPO_NAME}:${IMAGE_TAG}" \
  -f "${SCRIPT_DIR}/dockerfile" "${SCRIPT_DIR}"

echo ""
echo "=== Step 4: Tagging image ==="
docker tag "${ECR_REPO_NAME}:${IMAGE_TAG}" "${FULL_IMAGE_URI}"

echo ""
echo "=== Step 5: Pushing image to ECR ==="
docker push "${FULL_IMAGE_URI}"

echo ""
echo "Done! Image pushed to: ${FULL_IMAGE_URI}"
