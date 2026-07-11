# =============================================================================
# Container registry (ARM64 image lives here)
# =============================================================================
resource "aws_ecr_repository" "agent" {
  name                 = var.name
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

# =============================================================================
# S3 — system-of-record for downloaded reports (partitioned by company)
# =============================================================================
resource "aws_s3_bucket" "reports" {
  bucket = "${var.name}-${local.acct}"
}

resource "aws_s3_bucket_public_access_block" "reports" {
  bucket                  = aws_s3_bucket.reports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "reports" {
  bucket = aws_s3_bucket.reports.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "aws:kms" }
    bucket_key_enabled = true
  }
}

# =============================================================================
# DynamoDB — provenance (company, s3_key, source_url, hash, date, ...)
# =============================================================================
resource "aws_dynamodb_table" "provenance" {
  name         = "${var.name}-provenance"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "company"
  range_key    = "s3_key"

  attribute {
    name = "company"
    type = "S"
  }
  attribute {
    name = "s3_key"
    type = "S"
  }

  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true }
}

# =============================================================================
# IAM — AgentCore runtime execution role
# =============================================================================
data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.acct]
    }
  }
}

resource "aws_iam_role" "agent" {
  name               = "${var.name}-exec"
  assume_role_policy = data.aws_iam_policy_document.trust.json
}

data "aws_iam_policy_document" "agent" {
  statement {
    sid       = "WriteReports"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.reports.arn, "${aws_s3_bucket.reports.arn}/*"]
  }
  statement {
    sid       = "Provenance"
    actions   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.provenance.arn, "${aws_dynamodb_table.provenance.arn}/index/*"]
  }
  statement {
    sid       = "PullImage"
    actions   = ["ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage", "ecr:BatchCheckLayerAvailability", "ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:CreateLogGroup"]
    resources = ["*"]
  }
  # Bedrock model calls for the LLM relevance gate + query rewrite (Converse API).
  statement {
    sid = "InvokeModel"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = ["*"]
  }
  # Call the managed Web Search tool through the AgentCore Gateway (MCP, IAM auth).
  statement {
    sid       = "InvokeGateway"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = ["arn:aws:bedrock-agentcore:${local.region}:${local.acct}:gateway/*"]
  }
  # AgentCore Browser tool — start/stop/connect to a managed headless Chromium
  # session for in-AWS web browsing (renders JS, finds deep PDF links). Uses the
  # AWS-managed default browser (aws.browser.v1); no data leaves the account.
  statement {
    sid = "BrowserTool"
    actions = [
      "bedrock-agentcore:StartBrowserSession",
      "bedrock-agentcore:StopBrowserSession",
      "bedrock-agentcore:GetBrowserSession",
      "bedrock-agentcore:ListBrowserSessions",
      "bedrock-agentcore:ConnectBrowserAutomationStream",
      "bedrock-agentcore:ConnectBrowserLiveViewStream",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${local.region}:aws:browser/aws.browser.v1",
      "arn:aws:bedrock-agentcore:${local.region}:${local.acct}:browser-session/*",
    ]
  }
}

resource "aws_iam_role_policy" "agent" {
  name   = "agent"
  role   = aws_iam_role.agent.id
  policy = data.aws_iam_policy_document.agent.json
}

# =============================================================================
# Observability
# =============================================================================
resource "aws_cloudwatch_log_group" "agent" {
  name              = "/aws/bedrock-agentcore/${var.name}"
  retention_in_days = 90
}

# =============================================================================
# AgentCore Runtime + endpoint
#
# NOTE: these resources are new in the provider. If `plan` rejects an argument,
# check the registry docs for aws_bedrockagentcore_agent_runtime and adjust the
# nested block names — the rest of the stack is unaffected.
# =============================================================================
resource "aws_bedrockagentcore_agent_runtime" "agent" {
  agent_runtime_name = local.runtime_name
  role_arn           = aws_iam_role.agent.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = local.image_uri
    }
  }

  network_configuration {
    network_mode = "PUBLIC" # runtime needs egress to search + fetch reports
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  environment_variables = local.runtime_env

  # The aws_bedrockagentcore_agent_runtime provider does NOT reliably detect
  # image_tag / env changes as a diff, so subsequent applies report "no changes"
  # and the runtime stays on an old version. We ignore those attributes here and
  # let null_resource.runtime_update (below) force a new version via the CLI on
  # every change. This block only handles the INITIAL create.
  lifecycle {
    ignore_changes = [
      agent_runtime_artifact,
      environment_variables,
    ]
  }

  depends_on = [aws_iam_role_policy.agent, aws_cloudwatch_log_group.agent]
}

# Write the runtime env to a local JSON file (used by the CLI update below via
# file://). Sensitive so the values (API keys) never print in plan/state output.
resource "local_sensitive_file" "runtime_env" {
  filename = "${path.module}/.runtime-env.json"
  content  = jsonencode(local.runtime_env)
}

# ---------------------------------------------------------------------------
# Force a new runtime VERSION on every image_tag / env change.
# The native provider treats the runtime as unchanged when only the image tag or
# env vars change, leaving the DEFAULT endpoint pinned to an old version. This
# null_resource calls update-agent-runtime (which the provider can't express),
# creating a fresh version that the auto DEFAULT endpoint then tracks.
# ---------------------------------------------------------------------------
resource "null_resource" "runtime_update" {
  triggers = {
    runtime_id = aws_bedrockagentcore_agent_runtime.agent.agent_runtime_id
    region     = local.region
    role_arn   = aws_iam_role.agent.arn
    image_uri  = local.image_uri
    # re-run whenever any env value changes (hash of the rendered env file)
    env_hash = local_sensitive_file.runtime_env.content_sha256
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -uo pipefail
      RID="${self.triggers.runtime_id}"
      REGION="${self.triggers.region}"
      ROLE="${self.triggers.role_arn}"
      IMAGE="${self.triggers.image_uri}"
      ENV_FILE="${path.module}/.runtime-env.json"
      echo "Updating runtime $RID -> $IMAGE ($REGION) ..."
      OUT=$(aws bedrock-agentcore-control update-agent-runtime \
        --agent-runtime-id "$RID" \
        --region "$REGION" \
        --role-arn "$ROLE" \
        --agent-runtime-artifact "containerConfiguration={containerUri=$IMAGE}" \
        --network-configuration "networkMode=PUBLIC" \
        --environment-variables "file://$ENV_FILE" 2>&1)
      RC=$?
      if [ $RC -ne 0 ]; then echo "$OUT" >&2; exit $RC; fi
      VER=$(echo "$OUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('agentRuntimeVersion','?'))" 2>/dev/null || echo "?")
      echo "update-agent-runtime ok -> new version: $VER"
      echo "DEFAULT endpoint auto-tracks the latest version; invoke with qualifier DEFAULT."
    EOT
  }

  depends_on = [
    aws_bedrockagentcore_agent_runtime.agent,
    local_sensitive_file.runtime_env,
  ]
}

# NOTE: AgentCore auto-creates a "DEFAULT" endpoint that always tracks the latest
# runtime version, so we do NOT create a custom endpoint here. A custom endpoint
# would NOT auto-update on image bumps and would pin you to an old version.
# Invoke with qualifier "DEFAULT".
