#!/usr/bin/env bash
# =============================================================
#  discover_network.sh — find VPC + subnets for terraform.tfvars
#  Reads from the existing EC2 (i-061e79271c344ae84)
# =============================================================
set -euo pipefail

REGION="us-east-1"
INSTANCE_ID="i-061e79271c344ae84"

echo "=== Network details for EC2 $INSTANCE_ID ==="

VPC_ID=$(aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].VpcId' --output text)
EC2_SUBNET=$(aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].SubnetId' --output text)
EC2_AZ=$(aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].Placement.AvailabilityZone' --output text)

echo "VPC:        $VPC_ID"
echo "EC2 Subnet: $EC2_SUBNET ($EC2_AZ)"
echo ""
echo "All subnets in VPC (pick 2 in DIFFERENT AZs):"
aws ec2 describe-subnets --region "$REGION" \
  --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'Subnets[*].[SubnetId,AvailabilityZone,CidrBlock,MapPublicIpOnLaunch,Tags[?Key==`Name`].Value|[0]]' \
  --output table

SECOND_SUBNET=$(aws ec2 describe-subnets --region "$REGION" \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=state,Values=available" \
  --query "Subnets[?AvailabilityZone!='$EC2_AZ'] | [0].SubnetId" --output text)

echo ""
echo "=== Suggested terraform.tfvars values ==="
echo "vpc_id     = \"$VPC_ID\""
if [ "$SECOND_SUBNET" != "None" ] && [ -n "$SECOND_SUBNET" ]; then
  echo "subnet_ids = [\"$EC2_SUBNET\", \"$SECOND_SUBNET\"]"
else
  echo "subnet_ids = [\"$EC2_SUBNET\", \"<PICK_ANOTHER_AZ_SUBNET_FROM_TABLE_ABOVE>\"]"
fi
