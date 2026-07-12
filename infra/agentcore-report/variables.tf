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
  description = "Legacy gateway setting retained for existing tfvars. The registry-first runtime does not use managed web search."
  type        = number
  default     = 10
}

variable "enforce_site_domain" {
  description = "Legacy v1 setting retained for existing tfvars. v2 always validates against company.official_domains."
  type        = bool
  default     = true
}

variable "llm_model_id" {
  description = "Bedrock model/inference-profile used only to confirm deterministic document candidates. Required when require_llm_validation is true."
  type        = string
  default     = ""
}

variable "require_llm_validation" {
  description = "Fail closed unless Bedrock confirms the deterministic company, type, and year validation. Keep true for production."
  type        = bool
  default     = true
}

variable "sec_user_agent" {
  description = "Required for SEC EDGAR: organisation name plus monitored contact email, for example 'Report IQ ops@example.com'."
  type        = string
  default     = ""
}

variable "companies_house_api_key" {
  description = "Optional UK Companies House API key for annual-account retrieval."
  type        = string
  default     = ""
  sensitive   = true
}

variable "best_matches" {
  description = "Legacy v1 setting retained for existing tfvars. v2 stores at most one document per requests[] item."
  type        = number
  default     = 1
}

variable "google_api_key" {
  description = "Optional Google Programmable Search API key. Used only as a hard site-scoped fallback after registry and official-site discovery."
  type        = string
  default     = ""
  sensitive   = true
}

variable "serper_api_key" {
  description = "Legacy v1 setting retained for existing tfvars. The v2 runtime does not call Serper."
  type        = string
  default     = ""
  sensitive   = true
}

variable "use_browser" {
  description = "Enable the final AgentCore Browser / Playwright tier for JavaScript-rendered company report pages. It is used only after registry, site, and scoped-search discovery miss."
  type        = bool
  default     = false
}

variable "browser_identifier" {
  description = "AgentCore Browser identifier. The runtime currently uses the AWS-managed default aws.browser.v1."
  type        = string
  default     = "aws.browser.v1"
}

variable "browser_region" {
  description = "AWS Region hosting the AgentCore Browser. Keep this aligned with the runtime region unless Browser availability requires otherwise."
  type        = string
  default     = "us-east-1"
}

variable "enable_fargate_browser_worker" {
  description = "Deploy the optional SQS-driven Fargate browser worker for long-running dynamic-site attempts. It detects login/WAF/CAPTCHA and sends those jobs to manual review; it never bypasses them."
  type        = bool
  default     = false
}

variable "fargate_subnet_ids" {
  description = "Subnets for the Fargate browser worker. Required when enable_fargate_browser_worker is true; use private subnets with NAT for production, or explicitly approved public subnets for proof-of-concept use."
  type        = list(string)
  default     = []
}

variable "fargate_security_group_ids" {
  description = "Security groups for the Fargate browser worker. Required when enable_fargate_browser_worker is true."
  type        = list(string)
  default     = []
}

variable "fargate_assign_public_ip" {
  description = "Assign a public IP to the Fargate worker. Use false with private subnets and NAT in production."
  type        = bool
  default     = false
}

variable "google_cx" {
  description = "Optional. Google Programmable Search Engine ID (cx). Required with google_api_key."
  type        = string
  default     = ""
}

variable "brave_search_api_key" {
  description = "Optional Brave Search API key. Used only after registry and official-site discovery fail; returned URLs are hard-filtered to official company domains."
  type        = string
  default     = ""
  sensitive   = true
}

variable "enable_web_search" {
  description = <<-EOT
    Create the optional AgentCore Gateway + managed Web Search tool target for
    broad discovery elsewhere in the platform. The registry-first runtime does not
    use it because the backend cannot enforce site-scoped document retrieval. The gateway is made
    in Terraform; the web-search *target* is created via the AWS CLI from a
    null_resource because the provider has no connector-target resource yet.
    Requires AWS CLI v2 on the machine running `terraform apply`.
    Set false to avoid paying for unused gateway infrastructure.
  EOT
  type        = bool
  default     = false
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
