#!/usr/bin/env bash
# Build the agent as an ARM64 image and push to the ECR repo Terraform created.
# Usage: ./scripts/build_and_push.sh <account_id> <region> <ecr_repo_url> <tag>
#   ecr_repo_url = `terraform output -raw ecr_repository_url`
set -euo pipefail

ACCOUNT="${1:?account id}"; REGION="${2:?region}"; REPO="${3:?ecr repo url}"; TAG="${4:-v1}"
REGISTRY="${REPO%/*}"

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

echo ">> building ${REPO}:${TAG} (linux/arm64)"
# --load stores into local Docker daemon (always Docker v2 schema, never OCI).
# Lambda only accepts Docker v2; buildx --push produces OCI image index by default.
docker buildx build \
  --platform linux/arm64 \
  --provenance=false \
  --load \
  -t "${REPO}:${TAG}" \
  agent/

docker push "${REPO}:${TAG}"
echo "Pushed ${REPO}:${TAG}"
