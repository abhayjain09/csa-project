# ==========================================================================
# lambda.tf
# Isolated Vertex AI grounded-search Lambda — Tier 2 discovery engine for the
# Report IQ / EDO Co-Analyst download agent. Replaces the AgentCore managed
# WebSearch tool as the candidate-URL generator.
#
# This file is self-contained and additive. It references three objects that
# already exist in your root module (confirmed from terraform.tfstate):
#   * data.aws_region.current
#   * data.aws_caller_identity.current
#   * aws_iam_role.agent            (the AgentCore runtime exec role)
# It does NOT modify the agent runtime, gateway, S3, or provenance table.
#
# NOTE: the GCP service-account JSON key must already exist in Secrets Manager
# under var.gcp_secret_name (created out of band — never commit it to TF).
# ==========================================================================

# ─── Locals ──────────────────────────────────────────────────────────────────
locals {
  vertex_lambda_name = "edo-coanalyst-report-vertex-search"
  vertex_ecr_name    = "edo-coanalyst-report-vertex-search"

  # Tag set requested for this workload.
  csa_tags = {
    Environment = "NonProd"
    Name        = "CSA"
    contact     = "askdevopscloud@spglobal.com"
    AppID       = "ASP0017650"
    CreatedBy   = "Abhay.Lunkad"
    Owner       = "anuthama.c@spglobal.com"
  }
}

# ─── Variables ───────────────────────────────────────────────────────────────
variable "vertex_search_image_tag" {
  description = "Image tag for the Vertex search Lambda container (bump on each new push)."
  type        = string
  default     = "v1"
}

variable "vertex_model_id" {
  description = "Vertex generative model for grounded search. gemini-2.5-flash is cheap and plenty for URL discovery; switch to gemini-2.5-pro here if recall needs it."
  type        = string
  default     = "gemini-2.5-flash"
}

variable "vertex_location" {
  description = "Vertex AI region."
  type        = string
  default     = "us-central1"
}

variable "gcp_secret_name" {
  description = "Secrets Manager secret name passed to the Lambda as SecretId (name resolves fine and survives secret recreation)."
  type        = string
  default     = "GCP_Vertex_Service_Account_Key"
}

variable "gcp_secret_arn" {
  description = "Exact ARN of the GCP service-account key secret (used for the least-privilege IAM grant)."
  type        = string
  default     = "arn:aws:secretsmanager:us-east-1:610639371721:secret:GCP_Vertex_Service_Account_Key-cIW9TD"
}

# ─── ECR repo for the Lambda image ───────────────────────────────────────────
resource "aws_ecr_repository" "vertex_search" {
  name                 = local.vertex_ecr_name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = local.csa_tags
}

# ─── Lambda execution role ───────────────────────────────────────────────────
data "aws_iam_policy_document" "vertex_lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "vertex_lambda" {
  name               = "${local.vertex_lambda_name}-exec"
  assume_role_policy = data.aws_iam_policy_document.vertex_lambda_trust.json
  tags               = local.csa_tags
}

# Least-privilege: write its own logs + read ONLY the one GCP key secret.
data "aws_iam_policy_document" "vertex_lambda_perms" {
  statement {
    sid    = "Logs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.vertex_lambda_name}:*",
    ]
  }

  statement {
    sid       = "ReadGcpKey"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.gcp_secret_arn]
  }
}

resource "aws_iam_role_policy" "vertex_lambda" {
  name   = "vertex-search-perms"
  role   = aws_iam_role.vertex_lambda.id
  policy = data.aws_iam_policy_document.vertex_lambda_perms.json
}

# ─── Log group (explicit → managed retention + tags) ─────────────────────────
resource "aws_cloudwatch_log_group" "vertex_lambda" {
  name              = "/aws/lambda/${local.vertex_lambda_name}"
  retention_in_days = 90
  tags              = local.csa_tags
}

# ─── The Lambda function (container image) ───────────────────────────────────
resource "aws_lambda_function" "vertex_search" {
  function_name = local.vertex_lambda_name
  role          = aws_iam_role.vertex_lambda.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.vertex_search.repository_url}:${var.vertex_search_image_tag}"

  # No vpc_config on purpose: this function needs open egress to Vertex and to
  # resolve redirects; the account has no NAT gateway, so keeping it OUT of the
  # VPC gives it default internet access.

  timeout       = 900
  memory_size   = 512
  architectures = ["arm64"] # must match the docker build --platform linux/arm64 (see build_and_push.sh)

  environment {
    variables = {
      GCP_SECRET_NAME      = var.gcp_secret_name
      VERTEX_LOCATION      = var.vertex_location
      VERTEX_MODEL_ID      = var.vertex_model_id
      REDIRECT_WORKERS     = "8"
      REDIRECT_TIMEOUT     = "5"
      DEFAULT_MAX_RESULTS  = "10"
      IDENTITY_MAX_RESULTS = "8"
    }
  }

  # Optional hard cap so a heavy agent fan-out can't exhaust Vertex QPS quota
  # or the account concurrency pool. Leave commented unless you see throttling.
  # reserved_concurrent_executions = 10

  depends_on = [
    aws_iam_role_policy.vertex_lambda,
    aws_cloudwatch_log_group.vertex_lambda,
  ]

  tags = local.csa_tags
}

# ─── Let the AgentCore runtime role invoke this Lambda ───────────────────────
# Added as a SEPARATE inline policy on the existing agent role, so it does not
# touch or clobber the role's existing "agent" inline policy.
data "aws_iam_policy_document" "agent_invoke_vertex" {
  statement {
    sid       = "InvokeVertexSearchLambda"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.vertex_search.arn]
  }
}

resource "aws_iam_role_policy" "agent_invoke_vertex" {
  name   = "invoke-vertex-search"
  role   = aws_iam_role.agent.id
  policy = data.aws_iam_policy_document.agent_invoke_vertex.json
}

# ─── Outputs ─────────────────────────────────────────────────────────────────
output "vertex_search_lambda_name" {
  value       = aws_lambda_function.vertex_search.function_name
  description = "Set this as LAMBDA_SEARCH_FUNCTION in the agent runtime env."
}

output "vertex_search_lambda_arn" {
  value = aws_lambda_function.vertex_search.arn
}

output "vertex_search_ecr_url" {
  value = aws_ecr_repository.vertex_search.repository_url
}
