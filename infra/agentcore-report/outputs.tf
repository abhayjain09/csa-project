output "ecr_repository_url" {
  value = aws_ecr_repository.agent.repository_url
}

output "browser_worker_ecr_repository_url" {
  value = aws_ecr_repository.browser_worker.repository_url
}

output "browser_worker_jobs_queue_url" {
  value = var.enable_fargate_browser_worker ? aws_sqs_queue.browser_jobs[0].id : ""
}

output "browser_worker_results_queue_url" {
  value = var.enable_fargate_browser_worker ? aws_sqs_queue.browser_results[0].id : ""
}

output "region" {
  value = local.region
}

output "reports_bucket" {
  value = aws_s3_bucket.reports.id
}

output "provenance_table" {
  value = aws_dynamodb_table.provenance.name
}

output "agent_runtime_arn" {
  description = "Pass this to invoke from your laptop."
  value       = aws_bedrockagentcore_agent_runtime.agent.agent_runtime_arn
}

output "web_search_gateway_url" {
  description = "MCP endpoint of the Web Search gateway (empty if disabled)."
  value       = length(aws_bedrockagentcore_gateway.web_search) > 0 ? aws_bedrockagentcore_gateway.web_search[0].gateway_url : ""
}

output "invoke_example" {
  description = "Copy-paste to run once images are pushed and apply is complete."
  value = join("", [
    "aws bedrock-agentcore invoke-agent-runtime --region ", local.region,
    " --agent-runtime-arn ", aws_bedrockagentcore_agent_runtime.agent.agent_runtime_arn,
    " --qualifier DEFAULT --payload fileb://scripts/payload.example.json out.json && cat out.json",
  ])
}
