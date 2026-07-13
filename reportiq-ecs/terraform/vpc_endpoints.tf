# ════════════════════════════════════════════════════════════════════════════
#  VPC endpoints for Fargate in private subnets (no NAT gateway).
#  Uses an existing SG if present, creates a new one if not.
# ════════════════════════════════════════════════════════════════════════════

# ── Look up existing vpce SG (created earlier for SSM endpoints) ─────────────
data "aws_security_group" "existing_vpce" {
  count  = var.create_vpc_endpoints ? 1 : 0
  vpc_id = var.vpc_id

  filter {
    name   = "group-name"
    values = ["reportiq-vpce-sg"]
  }
}

# ── Create a fresh SG only if the existing one is not found ──────────────────
resource "aws_security_group" "vpce_new" {
  count = (var.create_vpc_endpoints && length(data.aws_security_group.existing_vpce) == 0) ? 1 : 0

  name        = "${local.name}-ecs-vpce-sg"
  description = "Report IQ ECS VPC interface endpoints"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTPS from ECS tasks and internal network"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.alb_ingress_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-ecs-vpce-sg" }
}

locals {
  # Use existing SG if found, otherwise use the newly created one
  vpce_sg_id = var.create_vpc_endpoints ? (
    length(data.aws_security_group.existing_vpce) > 0
    ? data.aws_security_group.existing_vpce[0].id
    : aws_security_group.vpce_new[0].id
  ) : ""

  interface_endpoints = var.create_vpc_endpoints ? {
    ecr_api = "com.amazonaws.${var.region}.ecr.api"
    ecr_dkr = "com.amazonaws.${var.region}.ecr.dkr"
    logs    = "com.amazonaws.${var.region}.logs"
  } : {}
}

# ── Interface endpoints (ecr.api, ecr.dkr, logs) ─────────────────────────────
resource "aws_vpc_endpoint" "interface" {
  for_each = local.interface_endpoints

  vpc_id              = var.vpc_id
  service_name        = each.value
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.subnet_ids
  security_group_ids  = [local.vpce_sg_id]
  private_dns_enabled = true

  tags = { Name = "${local.name}-${each.key}" }
}

# ── S3 gateway endpoint ───────────────────────────────────────────────────────
data "aws_route_tables" "selected" {
  count  = var.create_vpc_endpoints ? 1 : 0
  vpc_id = var.vpc_id
}

# Check if an S3 gateway endpoint already exists in this VPC
data "aws_vpc_endpoint" "existing_s3" {
  count        = var.create_vpc_endpoints ? 1 : 0
  vpc_id       = var.vpc_id
  service_name = "com.amazonaws.${var.region}.s3"
  state        = "available"
}

resource "aws_vpc_endpoint" "s3" {
  # Only create if no S3 endpoint already exists
  count = (var.create_vpc_endpoints && length(data.aws_vpc_endpoint.existing_s3) == 0) ? 1 : 0

  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = data.aws_route_tables.selected[0].ids

  tags = { Name = "${local.name}-s3-gw" }
}
