#!/usr/bin/env bash
# One-command deploy: build+push the ARM64 image, then terraform apply.
# The null_resource.runtime_update forces a new AgentCore runtime version from the
# new image on every run (the provider does not detect image/env changes itself).
#
# Usage: ./scripts/deploy.sh <tag>
#   e.g. ./scripts/deploy.sh v21
#
# Requires: terraform initialised, AWS creds, docker buildx.
set -euo pipefail

TAG="${1:?usage: ./scripts/deploy.sh <tag>   (e.g. v21)}"
REGION="$(terraform output -raw region 2>/dev/null || echo us-east-1)"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REPO="$(terraform output -raw ecr_repository_url)"

echo "==> 1/3 build + push ${REPO}:${TAG}"
./scripts/build_and_push.sh "$ACCOUNT" "$REGION" "$REPO" "$TAG"

echo "==> 2/3 terraform apply (image_tag=${TAG})"
terraform apply -auto-approve -var "image_tag=${TAG}"

echo "==> 3/3 confirm the DEFAULT endpoint advanced"
ID="$(terraform output -raw agent_runtime_arn | sed 's#.*/##')"
aws bedrock-agentcore-control list-agent-runtime-endpoints \
  --agent-runtime-id "$ID" --region "$REGION" \
  --query "runtimeEndpoints[].{name:name, live:liveVersion, status:status}" --output table

echo ""
echo "Deployed ${TAG}. Invoke with:"
echo "  python3 scripts/invoke_local.py \"\$(terraform output -raw agent_runtime_arn)\" scripts/payload.xylem.json --region=${REGION}"
