#!/usr/bin/env bash
# =============================================================================
# build_and_push.sh — Build the ARM64 container image and push it to ECR.
#
# Called by deploy.sh but can also be run standalone for image-only updates.
#
# Usage:
#   ./scripts/build_and_push.sh <account_id> <region> <ecr_repo_url> <tag>
#
# Example:
#   ./scripts/build_and_push.sh 123456789012 us-east-1 \
#       123456789012.dkr.ecr.us-east-1.amazonaws.com/pageindex-agent-dev v21
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
AGENT_DIR="${AGENT_DIR:-${ROOT_DIR}/agent}"

ACCOUNT="${1:?arg 1: aws account id}"
REGION="${2:?arg 2: aws region}"
REPO="${3:?arg 3: ecr repository url}"
TAG="${4:?arg 4: image tag}"

IMAGE_URI="${REPO}:${TAG}"

log()  { echo "[build_and_push] $*"; }
die()  { echo "[build_and_push] ERROR: $*" >&2; exit 1; }

# --------------------------------------------------------------------------
# Verify buildx has ARM64 support
# --------------------------------------------------------------------------

if ! docker buildx inspect --bootstrap 2>/dev/null | grep -q "linux/arm64"; then
    log "ARM64 platform not found in buildx. Installing via binfmt..."
    docker run --privileged --rm tonistiigi/binfmt --install arm64 \
        || die "Failed to install ARM64 binfmt emulation. On native ARM64 this is not needed."
fi

# --------------------------------------------------------------------------
# ECR login
# --------------------------------------------------------------------------

log "Authenticating Docker to ECR (${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com)..."
aws ecr get-login-password --region "${REGION}" \
    | docker login \
        --username AWS \
        --password-stdin \
        "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

# --------------------------------------------------------------------------
# Build (ARM64) and push
# --------------------------------------------------------------------------

log "Building ${IMAGE_URI} for linux/arm64 ..."
docker buildx build \
    --platform linux/arm64 \
    --tag "${IMAGE_URI}" \
    --push \
    --provenance=false \
    "${AGENT_DIR}"

# --provenance=false: avoids creating a multi-manifest index that some older
# ECR clients mis-parse. Remove if you want SLSA provenance attestations.

log "Pushed ${IMAGE_URI}"

# --------------------------------------------------------------------------
# Verify the image actually landed in ECR
# --------------------------------------------------------------------------

log "Verifying image in ECR..."
REPO_NAME="${REPO##*/}"   # strip registry prefix, keep repo name
aws ecr describe-images \
    --repository-name "${REPO_NAME}" \
    --image-ids "imageTag=${TAG}" \
    --region "${REGION}" \
    --query "imageDetails[0].{digest:imageDigest, pushed:imagePushedAt, size:imageSizeInBytes}" \
    --output table \
    || log "  Warning: could not verify image in ECR — check manually."
