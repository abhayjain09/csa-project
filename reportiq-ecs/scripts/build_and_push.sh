#!/usr/bin/env bash
# =============================================================
#  build_and_push.sh — build the Docker image and push to ECR
#  Run from your Mac (Apple Silicon builds ARM64 natively)
#
#  Usage:
#     ./build_and_push.sh [TAG]
#  TAG defaults to "latest"
# =============================================================
set -euo pipefail

REGION="us-east-1"
ACCOUNT="610639371721"
REPO_NAME="reportiq"
TAG="${1:-latest}"
ARCH="arm64"   # change to amd64 if you set cpu_architecture=X86_64 in tfvars

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$SCRIPT_DIR/../app"

ECR_URI="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE="${ECR_URI}/${REPO_NAME}:${TAG}"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC} $*"; }
step() { echo -e "\n${CYAN}=== $* ===${NC}"; }

step "1/4  Ensuring ECR repo exists"
aws ecr describe-repositories --region "$REGION" --repository-names "$REPO_NAME" >/dev/null 2>&1 \
  || aws ecr create-repository --region "$REGION" --repository-name "$REPO_NAME" \
       --image-scanning-configuration scanOnPush=true \
       --tags Key=AppID,Value=ASP0017650 Key=CreatedBy,Value=Abhay.Lunkad >/dev/null
info "Repo ready: $ECR_URI/$REPO_NAME"

step "2/4  Logging in to ECR"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$ECR_URI"
info "Logged in"

step "3/4  Building image ($ARCH)"
cd "$APP_DIR"
# buildx ensures correct platform; on Apple Silicon arm64 is native (fast)
docker buildx build \
  --platform "linux/${ARCH}" \
  --tag "$IMAGE" \
  --load \
  .
info "Built: $IMAGE"

step "4/4  Pushing to ECR"
docker push "$IMAGE"
info "Pushed: $IMAGE"

echo ""
echo -e "${GREEN}Image available at:${NC} $IMAGE"
echo ""
echo "Next: deploy with this tag —"
echo "  cd ../terraform && terraform apply -var=image_tag=${TAG}"
