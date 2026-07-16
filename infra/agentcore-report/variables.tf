variable "region" {
  description = "AWS region. us-east-1 has full AgentCore + managed Web Search availability."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Base name for resources."
  type        = string
  default     = "edo-coanalyst-report"
}

variable "image_tag" {
  description = "Container image tag to build/push and deploy. Bump per release."
  type        = string
  default     = "v1"
}

variable "web_search_max_results" {
  description = "maxResults passed to the managed Web Search tool (1-25)."
  type        = number
  default     = 10
}

variable "enforce_site_domain" {
  description = "When a query has a site: operator, only download results from that domain/subdomains (the managed Web Search tool ignores site:). Strongly recommended for compliance accuracy."
  type        = bool
  default     = true
}

variable "llm_model_id" {
  description = "Optional Bedrock model/inference-profile id for LLM query rewriting (e.g. a Claude model id). Empty = deterministic query prep only (relative years still resolved)."
  type        = string
  default     = ""
}

variable "best_matches" {
  description = "How many distinct documents to download per web_query (1 = just the single best match)."
  type        = number
  default     = 1
}

variable "google_api_key" {
  description = "Optional. Google Custom Search API key — enables a literal Google search (honors site:). Leave empty to use the managed Web Search tool."
  type        = string
  default     = ""
  sensitive   = true
}

variable "serper_api_key" {
  description = "Optional. Serper.dev API key — real Google SERP as JSON (returns deep PDF URLs, honors site:/filetype:). When set, this is the PRIMARY search provider. Get a key at https://serper.dev."
  type        = string
  default     = ""
  sensitive   = true
}

variable "use_browser" {
  description = "Use the AgentCore Browser tool (in-AWS headless Chromium) as the PRIMARY search provider. Renders JS and finds deep PDF links the managed search tool misses; no third-party data egress. Slower per query. Default false."
  type        = bool
  default     = false
}

variable "browser_identifier" {
  description = "AgentCore Browser identifier. Use the AWS-managed default 'aws.browser.v1' unless you've created a custom browser."
  type        = string
  default     = "aws.browser.v1"
}

variable "google_cx" {
  description = "Optional. Google Programmable Search Engine ID (cx). Required with google_api_key."
  type        = string
  default     = ""
}

variable "enable_web_search" {
  description = <<-EOT
    Create the AgentCore Gateway + managed Web Search tool target and point the
    agent at it (zero-egress, Amazon-indexed search over MCP). The gateway is made
    in Terraform; the web-search *target* is created via the AWS CLI from a
    null_resource because the provider has no connector-target resource yet.
    Requires AWS CLI v2 on the machine running `terraform apply`.
    Set false to deploy without it (agent falls back to direct search).
  EOT
  type        = bool
  default     = true
}

variable "gateway_search_tool" {
  description = "Exact name of the gateway's managed Web Search MCP tool to prefer for search."
  type        = string
  default     = "web-search-tool___WebSearch"
}

variable "gateway_strip_site" {
  description = "Reshape 'site:host term' queries into 'term host' before sending to the gateway's WebSearch backend, since it does not honor site: as a scope operator."
  type        = bool
  default     = true
}
