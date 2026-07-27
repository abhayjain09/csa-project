#!/usr/bin/env bash
# One-command deploy: build+push the ARM64 agent image, build+push the Vertex
# search Lambda image, then terraform apply (both image tags in one apply).
# The null_resource.runtime_update forces a new AgentCore runtime version from the
# new image on every run (the provider does not detect image/env changes itself).
#
# Usage: ./scripts/deploy.sh <agent_tag> [vertex_lambda_tag]
#   e.g. ./scripts/deploy.sh v21
#   e.g. ./scripts/deploy.sh v21 v3      (bump only the Vertex Lambda image)
#
# Requires: terraform initialised, AWS creds, docker buildx.
set -euo pipefail

TAG="${1:?usage: ./scripts/deploy.sh <agent_tag> [vertex_lambda_tag]   (e.g. v21)}"
VERTEX_TAG="${2:-$TAG}"

REGION="$(terraform output -raw region 2>/dev/null || echo us-east-1)"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REPO="$(terraform output -raw ecr_repository_url)"

echo "==> 1/4 build + push ${REPO}:${TAG}"
./scripts/build_and_push.sh "$ACCOUNT" "$REGION" "$REPO" "$TAG"

# ── Vertex search Lambda (isolated Tier 2 discovery engine) ──────────────────
# arm64: matches architectures = ["arm64"] in lambda.tf. Build context is
# ./vertex_search (lambda.py + requirements.txt + Dockerfile live together there).
# Must use buildx with --provenance=false --load, same as build_and_push.sh:
# plain `docker build` on modern Docker Desktop emits an OCI image index by
# default, which Lambda's CreateFunction rejects ("image manifest ... not
# supported") — Lambda only accepts the classic Docker v2 schema manifest
# that --load via buildx produces.
VERTEX_REPO="$(terraform output -raw vertex_search_ecr_url)"
echo "==> 2/4 build + push ${VERTEX_REPO}:${VERTEX_TAG}"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
docker buildx build \
  --platform linux/arm64 \
  --provenance=false \
  --load \
  -t "vertex-search:${VERTEX_TAG}" \
  ./vertex_search
docker tag  "vertex-search:${VERTEX_TAG}" "${VERTEX_REPO}:${VERTEX_TAG}"
docker push "${VERTEX_REPO}:${VERTEX_TAG}"

echo "==> 3/4 terraform apply (image_tag=${TAG}, vertex_search_image_tag=${VERTEX_TAG})"
terraform apply -auto-approve \
  -var "image_tag=${TAG}" \
  -var "vertex_search_image_tag=${VERTEX_TAG}"

echo "==> 4/4 confirm the DEFAULT endpoint advanced"
ID="$(terraform output -raw agent_runtime_arn | sed 's#.*/##')"
aws bedrock-agentcore-control list-agent-runtime-endpoints \
  --agent-runtime-id "$ID" --region "$REGION" \
  --query "runtimeEndpoints[].{name:name, live:liveVersion, status:status}" --output table

echo ""
echo "Vertex search Lambda: $(terraform output -raw vertex_search_lambda_name) @ ${VERTEX_TAG}"
echo ""
echo "Deployed ${TAG}. Invoke with:"
echo "  python3 scripts/invoke_local.py \"\$(terraform output -raw agent_runtime_arn)\" scripts/payload.xylem.json --region=${REGION}"