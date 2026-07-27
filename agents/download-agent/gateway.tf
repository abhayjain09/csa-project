# =============================================================================
# Managed Web Search tool on an AgentCore Gateway.
#
# The Gateway itself IS supported by the aws provider, so it's plain Terraform.
# The Web Search *target* uses connectorId "web-search", which the provider does
# NOT expose yet — so we create it with the AWS CLI from a null_resource, inside
# the same `terraform apply`. When the provider adds a connector target type,
# replace the null_resource with a native aws_bedrockagentcore_gateway_target.
#
# Requires: AWS CLI v2 on the machine running terraform, with the same creds.
# Gated by var.enable_web_search.
# =============================================================================

# ----------------------------- Gateway service role -------------------------
data "aws_iam_policy_document" "gateway_trust" {
  count = var.enable_web_search ? 1 : 0
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

resource "aws_iam_role" "gateway" {
  count              = var.enable_web_search ? 1 : 0
  name               = "${var.name}-gateway-svc"
  assume_role_policy = data.aws_iam_policy_document.gateway_trust[0].json
}

# Exact permissions required by the Web Search connector (per AWS docs):
#   InvokeGateway  + InvokeWebSearch on the service-owned tool ARN.
data "aws_iam_policy_document" "gateway_perms" {
  count = var.enable_web_search ? 1 : 0
  statement {
    sid       = "InvokeGateway"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = ["arn:aws:bedrock-agentcore:${local.region}:${local.acct}:gateway/*"]
  }
  statement {
    sid       = "InvokeWebSearch"
    actions   = ["bedrock-agentcore:InvokeWebSearch"]
    resources = ["arn:aws:bedrock-agentcore:${local.region}:aws:tool/web-search.v1"]
  }
}

resource "aws_iam_role_policy" "gateway" {
  count  = var.enable_web_search ? 1 : 0
  name   = "gateway-perms"
  role   = aws_iam_role.gateway[0].id
  policy = data.aws_iam_policy_document.gateway_perms[0].json
}

# ----------------------------- Gateway (Terraform) --------------------------
resource "aws_bedrockagentcore_gateway" "web_search" {
  count         = var.enable_web_search ? 1 : 0
  name          = "${var.name}-gw" # gateway names allow [0-9a-zA-Z-] only (no underscores)
  role_arn      = aws_iam_role.gateway[0].arn
  protocol_type = "MCP"

  protocol_configuration {
    mcp {
      instructions       = "Managed Web Search tool for the report-download agent."
      search_type        = "SEMANTIC" # if apply rejects this, use "DEFAULT"
      supported_versions = ["2025-11-25"]
    }
  }

  # IAM inbound auth: the agent SigV4-signs its MCP requests (no Cognito needed).
  authorizer_type = "AWS_IAM"
}

# ----------------------------- Web Search target (AWS CLI) ------------------
# Idempotent: create succeeds, or we treat a Conflict ("already exists") as ok.
resource "null_resource" "web_search_target" {
  count = var.enable_web_search ? 1 : 0

  triggers = {
    gateway_id = aws_bedrockagentcore_gateway.web_search[0].gateway_id
    region     = local.region
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -uo pipefail
      GID="${self.triggers.gateway_id}"
      REGION="${self.triggers.region}"
      echo "Creating web-search target on gateway $GID ($REGION) ..."
      OUT=$(aws bedrock-agentcore-control create-gateway-target \
        --gateway-identifier "$GID" \
        --region "$REGION" \
        --name "web-search-tool" \
        --target-configuration '{"mcp":{"connector":{"source":{"connectorId":"web-search"},"configurations":[{"name":"WebSearch","parameterValues":{}}]}}}' \
        --credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' 2>&1)
      RC=$?
      if [ $RC -eq 0 ]; then echo "created."; exit 0; fi
      if echo "$OUT" | grep -qiE 'conflict|already exist'; then echo "already exists, ok."; exit 0; fi
      echo "$OUT" >&2; exit $RC
    EOT
  }

  # Best-effort cleanup on destroy.
  provisioner "local-exec" {
    when        = destroy
    on_failure  = continue
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      GID="${self.triggers.gateway_id}"; REGION="${self.triggers.region}"
      TID=$(aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$GID" --region "$REGION" \
            --query "items[?name=='web-search-tool'].targetId | [0]" --output text 2>/dev/null || true)
      if [ -n "$TID" ] && [ "$TID" != "None" ]; then
        aws bedrock-agentcore-control delete-gateway-target --gateway-identifier "$GID" --target-id "$TID" --region "$REGION" || true
      fi
    EOT
  }

  depends_on = [aws_iam_role_policy.gateway]
}
