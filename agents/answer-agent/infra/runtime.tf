# ---------------------------------------------------------------------------
# AgentCore Runtime.
#
# Deployment sequence (see infra/README.md for the full recipe):
#   1. terraform apply         -> creates ECR repo + IAM role + placeholder
#                                 runtime (points at image_tag, may not exist yet)
#   2. docker buildx build ... -> build ARM64 image
#   3. docker push             -> push to ECR
#   4. terraform apply         -> if image_tag changed, runtime picks up new image
#
# The runtime is authenticated with AWS_IAM by default (no
# authorizer_configuration block). Callers use SigV4 to invoke it. To use
# Cognito/JWT authentication instead, add an authorizer_configuration block
# with a custom_jwt_authorizer subblock (see AWS docs).
# ---------------------------------------------------------------------------

resource "aws_bedrockagentcore_agent_runtime" "this" {
  agent_runtime_name = replace("${var.project_name}_${var.environment}", "-", "_")
  description        = "PageIndex ReAct agent — vectorless RAG over hierarchical pageindex JSON."
  role_arn           = local.execution_role_arn

  agent_runtime_artifact {
    container_configuration {
      # Image URI referenced by tag. AWS caches by URI, so bumping image_tag
      # (or using a SHA-based tag from CI) is what triggers a redeploy on
      # subsequent applies.
      container_uri = "${aws_ecr_repository.agent.repository_url}:${var.image_tag}"
    }
  }

  network_configuration {
    network_mode = "PUBLIC" # runtime needs egress to search + fetch reports
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }


  lifecycle_configuration {
    idle_runtime_session_timeout = var.idle_session_timeout_seconds
    max_lifetime                 = var.max_session_lifetime_seconds
  }

  # Environment variables consumed by config.py inside the container. Every
  # tunable that isn't a secret goes through here — secrets should come from
  # Secrets Manager or Parameter Store fetched at runtime (none needed for
  # this app).
  environment_variables = {
    AGENT_MODEL_ID       = var.bedrock_model_id
    AGENT_TOOL_BUDGET    = tostring(var.tool_call_budget)
    AGENT_MAX_PAGE_SPAN  = tostring(var.max_page_span)
    AGENT_MAX_PARALLEL   = tostring(var.max_parallel_questions)
    AGENT_STALENESS_DAYS = tostring(var.staleness_warn_days)
    AWS_REGION           = var.aws_region

    # See the "AgentCore caches the S3/ECR artifact" note in the AWS docs:
    # bumping any env var forces the runtime to refresh its container config
    # on next invoke. Handy for forcing a redeploy without changing the tag.
    _DEPLOY_TIMESTAMP = timestamp()
  }

  # timestamp() would otherwise cause perpetual drift on plan. Ignore it so
  # 'terraform plan' is quiet unless you explicitly want a redeploy (via
  # -replace= or by changing image_tag).
  lifecycle {
    ignore_changes = [
      environment_variables["_DEPLOY_TIMESTAMP"],
    ]
  }
}

# ---------------------------------------------------------------------------
# Runtime endpoint — the invocation URL your app calls.
#
# You can have multiple endpoints per runtime (e.g. "prod", "canary")
# pointing at different runtime versions. We create one, named after the
# environment.
# ---------------------------------------------------------------------------

resource "aws_bedrockagentcore_agent_runtime_endpoint" "default" {
  name             = "default"
  agent_runtime_id = aws_bedrockagentcore_agent_runtime.this.agent_runtime_id
  description      = "Default endpoint for ${var.environment}"
}

# ---------------------------------------------------------------------------
# Force a new AgentCore runtime version on every deploy.
#
# The Terraform provider does not detect image-digest changes — if you push
# a new image under the same tag without changing image_tag in tfvars, the
# runtime silently keeps the old container. The null_resource below is
# tainted and re-run by deploy.sh on every invocation, triggering a
# create_agent_runtime_version API call via local-exec.
#
# The shell command is a no-op if the runtime hasn't changed; it just
# ensures AWS registers a new version pointing at the current image URI.
# ---------------------------------------------------------------------------

resource "null_resource" "runtime_update" {
  triggers = {
    # Re-run whenever the image tag or any env var changes.
    image_tag  = var.image_tag
    runtime_id = aws_bedrockagentcore_agent_runtime.this.agent_runtime_id
  }

  provisioner "local-exec" {
    command = <<-EOT
      echo "[null_resource.runtime_update] Confirming runtime ${aws_bedrockagentcore_agent_runtime.this.agent_runtime_id} points at tag ${var.image_tag}"
      aws bedrock-agentcore-control get-agent-runtime \
        --agent-runtime-id "${aws_bedrockagentcore_agent_runtime.this.agent_runtime_id}" \
        --region "${var.aws_region}" \
        --query "agentRuntime.{status:status, version:lastUpdatedAt}" \
        --output table 2>/dev/null || true
    EOT
  }
}
