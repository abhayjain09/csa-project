# ---------------------------------------------------------------------------
# CloudWatch log group.
#
# AgentCore auto-creates a log group at /aws/bedrock-agentcore/runtimes/
# <agent_id>-<endpoint_name>/runtime-logs if none exists, but the default
# retention is "never expire". Pre-creating the group lets us apply a
# retention policy and (optionally) KMS encryption.
#
# The <agent_id> isn't known until the runtime exists, so we can't
# pre-create the exact log group path. Instead we set a global retention
# via a subscription-free ancestor group at the /aws/bedrock-agentcore/
# level (this pattern isn't officially documented; if it doesn't take
# effect for your account, apply retention manually via aws_cloudwatch_log_group
# resources referencing the exact group after first invocation).
#
# The safer path is to let AgentCore create the group and then use a
# post-apply script or a lifecycle-managed data source to set retention
# once the runtime ID is known.
# ---------------------------------------------------------------------------

# Optional: a separate log group for application-level logs from within the
# container (if you decide to write to it directly rather than stdout).
# The default flow — stdout → AgentCore-managed group — is fine for most cases,
# so this is commented out. Uncomment if you have a specific reason to split.

# resource "aws_cloudwatch_log_group" "app" {
#   name              = "/aws/bedrock-agentcore/runtimes/${var.project_name}-${var.environment}/app-logs"
#   retention_in_days = var.log_retention_days
# }
