#!/usr/bin/env bash
# Build the PageIndex runtime image for ARM64 (AgentCore is Graviton-only)
# and push it to ECR.
# Called by deploy.sh — can also be run standalone for faster iteration.
#
# Usage: ./scripts/build_and_push.sh <account> <region> <repo_url> <tag>
set -euo pipefail

ACCOUNT="${1:?missing account}"
REGION="${2:?missing region}"
REPO="${3:?missing repo_url}"
TAG="${4:?missing tag}"

echo "  [build_and_push] account=${ACCOUNT} region=${REGION} repo=${REPO} tag=${TAG}"

# Authenticate Docker to ECR
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REPO"

# Build from the repo root (where Dockerfile lives) — pageindex-lib/ must
# already be populated with the cloned PageIndex repo.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# CRITICAL: AgentCore Runtime is ARM64-only (AWS Graviton)
docker buildx build \
  --platform linux/arm64 \
  --tag "${REPO}:${TAG}" \
  --tag "${REPO}:latest" \
  --push \
  "${REPO_ROOT}"

echo "  [build_and_push] pushed ${REPO}:${TAG}"

