#!/usr/bin/env bash
# Build the x86_64 Fargate browser worker and push it to its ECR repository.
# Usage: ./scripts/build_and_push_browser_worker.sh <region> <ecr_repo_url> <tag>
set -euo pipefail

REGION="${1:?region}"
REPO="${2:?ecr repo url}"
TAG="${3:-v1}"
REGISTRY="${REPO%/*}"

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

echo ">> building ${REPO}:${TAG} (linux/amd64)"
docker buildx build --platform linux/amd64 -f worker/Dockerfile -t "${REPO}:${TAG}" --push .

echo "Pushed ${REPO}:${TAG}"
