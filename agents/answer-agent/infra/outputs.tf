output "ecr_repository_url" {
  description = "ECR repo URL. Use for `docker buildx build ... --tag <url>:<tag> --push`."
  value       = aws_ecr_repository.agent.repository_url
}

output "ecr_repository_arn" {
  description = "ECR repo ARN."
  value       = aws_ecr_repository.agent.arn
}

output "runtime_id" {
  description = "AgentCore Runtime ID. Useful for CloudWatch log lookups: /aws/bedrock-agentcore/runtimes/<id>-<endpoint>/runtime-logs"
  value       = aws_bedrockagentcore_agent_runtime.this.agent_runtime_id
}

output "runtime_arn" {
  description = "AgentCore Runtime ARN."
  value       = aws_bedrockagentcore_agent_runtime.this.agent_runtime_arn
}

output "runtime_endpoint_arn" {
  description = "Endpoint ARN — pass this to InvokeAgentRuntime API."
  value       = aws_bedrockagentcore_agent_runtime_endpoint.default.agent_runtime_endpoint_arn
}

output "runtime_role_arn" {
  description = "Execution role ARN attached to the runtime (created or existing)."
  value       = local.execution_role_arn
}

output "role_created_by_terraform" {
  description = "True if Terraform created the IAM role. False if an existing role was supplied via existing_role_arn."
  value       = local.create_role
}

output "input_bucket" {
  description = "S3 bucket where the runtime reads pageindex + questionnaire assets."
  value       = local.input_bucket_name
}

output "cloudwatch_log_group_prefix" {
  description = "CloudWatch log group prefix for runtime logs. Full path is <prefix>/<runtime_id>-<endpoint>/runtime-logs."
  value       = "/aws/bedrock-agentcore/runtimes"
}

# Convenience string for the docker buildx one-liner.
output "docker_push_command" {
  description = "Copy-paste command to build & push the ARM64 image."
  value = join(" ", [
    "docker buildx build",
    "--platform linux/arm64",
    "--tag ${aws_ecr_repository.agent.repository_url}:${var.image_tag}",
    "--push",
    "../agent"
  ])
}

output "region" {
  description = "AWS region all resources are deployed in."
  value       = var.aws_region
}

output "agent_runtime_arn" {
  description = "Alias for runtime_arn — used by deploy/invoke scripts."
  value       = aws_bedrockagentcore_agent_runtime.this.agent_runtime_arn
}
