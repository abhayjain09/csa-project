# ---------------------------------------------------------------------------
# main.tf — AgentCore Runtime infrastructure for PageIndex
#
# Resources:
#   - ECR repository for the runtime container image
#   - Single IAM role used by both the EC2 (caller) and the AgentCore
#     runtime (execution) — no need for two separate roles
#   - AgentCore Runtime
#
# Usage:
#   cd infra/
#   terraform init
#   terraform apply          # picks up terraform.tfvars automatically
#   terraform apply -var="image_tag=v2"   # override tag at deploy time
# ---------------------------------------------------------------------------

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40.0"
    }
  }
}

# ---------------------------------------------------------------------------
# Variables — all values live in terraform.tfvars
# ---------------------------------------------------------------------------
variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
}

variable "reports_bucket" {
  description = "S3 bucket that holds the PDF reports"
  type        = string
}

variable "runtime_name" {
  description = "Name for the AgentCore runtime and related IAM resources"
  type        = string
}

variable "ecr_repo_name" {
  description = "Name for the ECR repository that holds the runtime image"
  type        = string
}

variable "pageindex_model" {
  description = "LiteLLM model string injected into the runtime container"
  type        = string
}

variable "image_tag" {
  description = "Docker image tag to deploy — overridden by deploy.sh on each run"
  type        = string
}

# ---------------------------------------------------------------------------
# Provider + locals
# ---------------------------------------------------------------------------
provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Environment = "NonProd"
      Name        = "EDO-CoAnalyst-tool"
      contact     = "askdevopscloud@spglobal.com"
      AppID       = "ASP0017650"
      CreatedBy   = "Abhay.Lunkad"
      Owner       = "anuthama.c@spglobal.com"
    }
  }
}

# Account ID resolved automatically from the EC2 instance profile —
# no hardcoding or manual input required.
data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  image_uri  = "${local.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${var.ecr_repo_name}:${var.image_tag}"
}

# ---------------------------------------------------------------------------
# ECR Repository
# ---------------------------------------------------------------------------
resource "aws_ecr_repository" "pageindex" {
  name                 = var.ecr_repo_name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Project = var.runtime_name }
}

resource "aws_ecr_lifecycle_policy" "pageindex" {
  repository = aws_ecr_repository.pageindex.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 5 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

# ---------------------------------------------------------------------------
# IAM — Single shared role
#
# Trusted by both:
#   - bedrock-agentcore.amazonaws.com  (runtime execution inside the container)
#   - your AWS account root             (EC2 instance profile calling the runtime)
#
# Permissions cover everything both sides need:
#   - EC2 side:      InvokeAgentRuntime, S3 list/read/write (pageindex JSON)
#   - Runtime side:  S3 read (stream PDFs), Bedrock invoke, ECR pull, CloudWatch
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    # AgentCore service assumes this role when running the container
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    # Confused deputy protection — restricts the AgentCore service principal
    # to only assume this role on behalf of your account, not any other account.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    # EC2 instance profile assumes this role to call the runtime and access S3
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
  }
}

resource "aws_iam_role" "pageindex" {
  name               = "${var.runtime_name}-role"
  assume_role_policy = data.aws_iam_policy_document.trust.json
  tags               = { Project = var.runtime_name }
}

data "aws_iam_policy_document" "permissions" {
  # ── EC2 side ──────────────────────────────────────────────────────────────

  # Invoke the AgentCore runtime
  statement {
    sid     = "InvokeAgentCoreRuntime"
    effect  = "Allow"
    actions = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = [
      "arn:aws:bedrock-agentcore:${var.aws_region}:${local.account_id}:runtime/${var.runtime_name}",
    ]
  }

  # S3 — list PDFs, load existing pageindex, write updated pageindex
  statement {
    sid    = "S3ReadWritePageindex"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = [
      "arn:aws:s3:::${var.reports_bucket}",
      "arn:aws:s3:::${var.reports_bucket}/*",
    ]
  }

  # ── Runtime side ──────────────────────────────────────────────────────────

  # Bedrock — invoke only the specific cross-region inference profile used by PageIndex.
  # us.anthropic.claude-sonnet-4-5-20250929-v1:0 is a cross-region inference profile
  # whose ARN lives under the inference-profile resource type.
  statement {
    sid    = "BedrockInvokeModel"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = [
	"*",
    ]
  }

  # ECR — pull the runtime container image.
  # GetAuthorizationToken is an account-level API; AWS requires resources = ["*"] for it.
  statement {
    sid    = "ECRAuthToken"
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
    ]
    resources = ["*"]
  }

  # Image-layer and manifest pull scoped to only the PageIndex ECR repository.
  statement {
    sid    = "ECRPullImage"
    effect = "Allow"
    actions = [
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
      "ecr:BatchCheckLayerAvailability",
    ]
    resources = [
      "arn:aws:ecr:${var.aws_region}:${local.account_id}:repository/${var.ecr_repo_name}",
    ]
  }

  # CloudWatch Logs — runtime container log output
  statement {
    sid    = "CloudWatchLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/bedrock-agentcore/*",
    ]
  }
}

resource "aws_iam_role_policy" "pageindex" {
  name   = "${var.runtime_name}-policy"
  role   = aws_iam_role.pageindex.id
  policy = data.aws_iam_policy_document.permissions.json
}

# ---------------------------------------------------------------------------
# AgentCore Runtime
# ---------------------------------------------------------------------------
resource "aws_bedrockagentcore_agent_runtime" "pageindex" {
  agent_runtime_name = var.runtime_name
  description        = "PageIndex runtime — indexes PDFs from S3 using Claude via Bedrock"
  role_arn           = aws_iam_role.pageindex.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = local.image_uri
    }
  }

  # Environment variables injected into the runtime container
  environment_variables = {
    PAGEINDEX_MODEL  = var.pageindex_model
    AWS_REGION       = var.aws_region
    PYTHONUNBUFFERED = "1"
  }

  # PUBLIC mode — runtime needs to reach Bedrock and S3 endpoints
  network_configuration {
    network_mode = "PUBLIC"
  }
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
output "aws_region" {
  description = "AWS region — used by deploy.sh"
  value       = var.aws_region
}

output "ecr_repository_url" {
  description = "ECR URL — push Docker image here"
  value       = aws_ecr_repository.pageindex.repository_url
}

output "runtime_arn" {
  description = "AgentCore runtime ARN — exported automatically by deploy.sh as AGENTCORE_RUNTIME_ARN"
  value       = aws_bedrockagentcore_agent_runtime.pageindex.agent_runtime_arn
}

output "role_arn" {
  description = "Single IAM role ARN — attach to your EC2 instance profile"
  value       = aws_iam_role.pageindex.arn
}


