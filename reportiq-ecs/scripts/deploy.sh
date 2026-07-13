#!/usr/bin/env bash
# =============================================================
#  deploy.sh — full one-shot ECS deploy from your Mac
#
#  Usage:  ./deploy.sh [TAG]
#
#  1. terraform init
#  2. apply -target ECR (repo must exist before push)
#  3. build + push image
#  4. full apply (ECS, ALB, HTTPS cert, VPC endpoints)
#
#  Prereqs: terraform/terraform.tfvars filled, Docker running, AWS auth.
# =============================================================
set -euo pipefail

TAG="${1:-v$(date +%Y%m%d-%H%M%S)}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TF_DIR="$SCRIPT_DIR/../terraform"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC} $*"; }
step() { echo -e "\n${CYAN}═══ $* ═══${NC}"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }

[ -f "$TF_DIR/terraform.tfvars" ] || { warn "Create terraform/terraform.tfvars first (copy the .example)"; exit 1; }
docker info >/dev/null 2>&1 || { warn "Docker not running"; exit 1; }

step "1/4  terraform init"
cd "$TF_DIR"; terraform init -input=false

step "2/4  Create ECR repo"
terraform apply -input=false -auto-approve -target=aws_ecr_repository.app

step "3/4  Build & push image ($TAG)"
"$SCRIPT_DIR/build_and_push.sh" "$TAG"

step "4/4  Full apply"
cd "$TF_DIR"
terraform apply -input=false -auto-approve -var="image_tag=$TAG"

echo ""
step "Done"
echo -e "${GREEN}Portal:${NC} $(terraform output -raw portal_url)"
echo ""
echo "(Self-signed cert → browser warns once; click Advanced → Proceed.)"
echo ""
echo "Watch it come up:"
echo "  aws ecs wait services-stable --cluster \$(terraform output -raw ecs_cluster) \\"
echo "    --services \$(terraform output -raw ecs_service) --region us-east-1"
echo ""
echo "Target health:"
echo "  aws elbv2 describe-target-health --target-group-arn \$(terraform output -raw target_group_arn) \\"
echo "    --region us-east-1 --query 'TargetHealthDescriptions[*].[Target.Id,TargetHealth.State]' --output table"
echo ""
echo "Logs:"
echo "  aws logs tail \$(terraform output -raw log_group) --follow --region us-east-1"
