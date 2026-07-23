# ---------------------------------------------------------------------------
# IAM execution role for the AgentCore runtime.
#
# CONDITIONAL — all resources here are skipped when var.existing_role_arn
# is set. In that case the caller supplies a pre-existing role and is
# responsible for ensuring it carries the required permissions listed in
# variables.tf and in the README.
#
# When var.existing_role_arn is empty (default), Terraform creates:
#   - One IAM role  (aws_iam_role.runtime)
#   - Four inline policies attached to it:
#       ecr_pull, bedrock_invoke, s3_read, observability
# ---------------------------------------------------------------------------

locals {
  # True  → create everything in this file.
  # False → skip everything; use the caller-supplied role ARN.
  create_role = var.existing_role_arn == ""

  # The ARN used everywhere else (runtime.tf, outputs.tf).
  # Resolves to whichever role is actually in use.
  execution_role_arn = local.create_role ? aws_iam_role.runtime[0].arn : var.existing_role_arn
}

# ---------------------------------------------------------------------------
# Trust policy (only rendered when we are creating the role)
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "runtime_assume" {
  count = local.create_role ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    # Scope: only calls from THIS account.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    # Scope: only when acting on behalf of an AgentCore runtime in this account.
    # Cannot reference the specific runtime ARN (circular dependency), so we
    # use a wildcard scoped to the AgentCore service ARN pattern.
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values = [
        "arn:aws:bedrock-agentcore:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:runtime/*"
      ]
    }
  }
}

# ---------------------------------------------------------------------------
# Role
# ---------------------------------------------------------------------------

resource "aws_iam_role" "runtime" {
  count = local.create_role ? 1 : 0

  name               = "${var.project_name}-${var.environment}-runtime"
  description        = "Execution role for the ${var.project_name} AgentCore runtime"
  assume_role_policy = data.aws_iam_policy_document.runtime_assume[0].json
}

# ---------------------------------------------------------------------------
# Inline policy: ECR pull
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ecr_pull" {
  count = local.create_role ? 1 : 0

  statement {
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPull"
    effect = "Allow"
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchCheckLayerAvailability",
    ]
    resources = [aws_ecr_repository.agent.arn]
  }
}

resource "aws_iam_role_policy" "ecr_pull" {
  count = local.create_role ? 1 : 0

  name   = "ecr-pull"
  role   = aws_iam_role.runtime[0].id
  policy = data.aws_iam_policy_document.ecr_pull[0].json
}

# ---------------------------------------------------------------------------
# Inline policy: Bedrock Converse
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "bedrock_invoke" {
  count = local.create_role ? 1 : 0

  statement {
    sid    = "InvokeModel"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:Converse",
      "bedrock:ConverseStream",
    ]
    resources = [
      "*",
    ]
  }
}

resource "aws_iam_role_policy" "bedrock_invoke" {
  count = local.create_role ? 1 : 0

  name   = "bedrock-invoke"
  role   = aws_iam_role.runtime[0].id
  policy = data.aws_iam_policy_document.bedrock_invoke[0].json
}

# ---------------------------------------------------------------------------
# Inline policy: S3 read (input bucket)
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "s3_read" {
  count = local.create_role ? 1 : 0

  statement {
    sid    = "S3GetHead"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
    ]
    resources = ["${local.input_bucket_arn}/*"]
  }

  statement {
    sid       = "S3List"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [local.input_bucket_arn]
  }
}

resource "aws_iam_role_policy" "s3_read" {
  count = local.create_role ? 1 : 0

  name   = "s3-read"
  role   = aws_iam_role.runtime[0].id
  policy = data.aws_iam_policy_document.s3_read[0].json
}

# ---------------------------------------------------------------------------
# Inline policy: observability (CloudWatch logs + X-Ray)
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "observability" {
  count = local.create_role ? 1 : 0

  statement {
    sid    = "CloudWatchLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
      "logs:DescribeLogGroups",
    ]
    resources = [
      "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/*",
    ]
  }

  statement {
    sid    = "CloudWatchTelemetry"
    effect = "Allow"
    actions = [
      "cloudwatch:PutMetricData",
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "observability" {
  count = local.create_role ? 1 : 0

  name   = "observability"
  role   = aws_iam_role.runtime[0].id
  policy = data.aws_iam_policy_document.observability[0].json
}
