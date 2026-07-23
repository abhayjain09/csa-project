#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# --- Configuration ---
AWS_ACCOUNT_ID="610639371721"
AWS_REGION="us-east-1"
ECR_REPO_NAME="gcp-vertex-lambda"
IMAGE_TAG="latest"
LAMBDA_FUNCTION_NAME="bedrock-google-search-bridge"
LAMBDA_EXEC_ROLE_ARN="arn:aws:iam::610639371721:role/service-role/bedrock-google-search-bridge-role-x3ujdt3t"
ARCH="arm64"
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
echo "=== Step 3: Building Docker image for ${ARCH} via isolated buildx builder (forced docker format) ==="
docker buildx create --name lambdabuilder --driver docker-container --use 2>/dev/null || docker buildx use lambdabuilder
docker buildx build --platform "linux/${ARCH}" \
  --no-cache \
  --provenance=false \
  --output type=docker \
  -t "${ECR_REPO_NAME}:${IMAGE_TAG}" \
  -f "${SCRIPT_DIR}/dockerfile" "${SCRIPT_DIR}" \
  --load

echo ""
echo "=== Step 4: Tagging image ==="
docker tag "${ECR_REPO_NAME}:${IMAGE_TAG}" "${FULL_IMAGE_URI}"

echo ""
echo "=== Step 5: Pushing image to ECR ==="
docker push "${FULL_IMAGE_URI}"

echo ""
echo "=== Step 5b: Verifying manifest media type (must be docker v2, not OCI) ==="
MEDIA_TYPE=$(docker manifest inspect "${FULL_IMAGE_URI}" | grep -m1 '"mediaType"' | head -1 || true)
echo "Detected: ${MEDIA_TYPE}"
if echo "${MEDIA_TYPE}" | grep -qi "oci"; then
  echo "ERROR: Image is still in OCI format. Lambda may reject this."
  echo "Fix: disable containerd image store in Docker Desktop (Settings > General),"
  echo "then run: docker buildx prune -af   and re-run this script."
  exit 1
fi

echo ""
echo "=== Step 6: Deploying to Lambda (arch: ${ARCH}) ==="
CURRENT_ARCH=""
if aws lambda get-function --function-name "${LAMBDA_FUNCTION_NAME}" --region "${AWS_REGION}" > /dev/null 2>&1; then
  CURRENT_ARCH=$(aws lambda get-function-configuration \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}" \
    --query 'Architectures[0]' --output text)
  echo "Existing function found with architecture: ${CURRENT_ARCH}"
fi

if [ -n "${CURRENT_ARCH}" ] && [ "${CURRENT_ARCH}" != "${ARCH}" ]; then
  echo "Architecture mismatch (${CURRENT_ARCH} -> ${ARCH}). Architecture is immutable — deleting and recreating function..."
  aws lambda delete-function \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}"

  echo "Creating function fresh with ${ARCH}..."
  aws lambda create-function \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --package-type Image \
    --code ImageUri="${FULL_IMAGE_URI}" \
    --role "${LAMBDA_EXEC_ROLE_ARN}" \
    --architectures "${ARCH}" \
    --timeout 30 \
    --memory-size 256 \
    --region "${AWS_REGION}"

elif [ -n "${CURRENT_ARCH}" ] && [ "${CURRENT_ARCH}" == "${ARCH}" ]; then
  echo "Architecture already matches — updating code only..."
  aws lambda update-function-code \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --image-uri "${FULL_IMAGE_URI}" \
    --region "${AWS_REGION}"

  aws lambda wait function-updated \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}"

else
  echo "Function does not exist — creating fresh with ${ARCH}..."
  aws lambda create-function \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --package-type Image \
    --code ImageUri="${FULL_IMAGE_URI}" \
    --role "${LAMBDA_EXEC_ROLE_ARN}" \
    --architectures "${ARCH}" \
    --timeout 30 \
    --memory-size 256 \
    --region "${AWS_REGION}"
fi

echo ""
echo "Done! Image pushed to: ${FULL_IMAGE_URI}"
echo "Lambda function '${LAMBDA_FUNCTION_NAME}' is now using this image (${ARCH})."