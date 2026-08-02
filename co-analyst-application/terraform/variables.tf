variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "account_id" {
  description = "AWS account ID"
  type        = string
  default     = "610639371721"
}

# ─── Networking (MUST be set in terraform.tfvars) ────────────────────────────
variable "vpc_id" {
  description = "VPC ID where the ALB and ECS tasks run"
  type        = string
}

variable "subnet_ids" {
  description = "At least 2 subnet IDs in different AZs (for ALB + tasks). Internal subnets."
  type        = list(string)
}

# ─── App / image ─────────────────────────────────────────────────────────────
variable "app_name" {
  description = "Base name for all resources"
  type        = string
  default     = "reportiq"
}

variable "image_tag" {
  description = "Container image tag to deploy (set by build script)"
  type        = string
  default     = "latest"
}

variable "cpu" {
  description = "Fargate task CPU units (256, 512, 1024, 2048, 4096)"
  type        = number
  default     = 512
}

variable "memory" {
  description = "Fargate task memory in MiB"
  type        = number
  default     = 1024
}

variable "desired_count" {
  description = "Number of running tasks"
  type        = number
  default     = 1
}

variable "bulk_company_concurrency" {
  description = "Maximum companies executed concurrently within one bulk submission"
  type        = number
  default     = 3

  validation {
    condition     = var.bulk_company_concurrency >= 1 && var.bulk_company_concurrency <= 10
    error_message = "bulk_company_concurrency must be between 1 and 10."
  }
}

variable "cpu_architecture" {
  description = "X86_64 or ARM64 (must match the image you build)"
  type        = string
  default     = "X86_64"
}

variable "assign_public_ip" {
  description = "Whether Fargate tasks get a public IP (false for private subnets with NAT/endpoints)"
  type        = bool
  default     = false
}

variable "enable_browser_worker" {
  description = "Run one persistent queue-driven Fargate browser service for typed blocked_by_source_waf results"
  type        = bool
  default     = false
}

variable "browser_jobs_table" {
  description = "DynamoDB table used for durable browser fallback job state"
  type        = string
  default     = "reportiq-browser-jobs"
}

variable "browser_worker_cpu" {
  description = "CPU units for the persistent Chromium Fargate service"
  type        = number
  default     = 1024
}

variable "browser_worker_memory" {
  description = "Memory in MiB for the persistent Chromium Fargate service"
  type        = number
  default     = 2048
}

variable "browser_worker_subnet_ids" {
  description = "Subnets for browser tasks; defaults to subnet_ids. They must have approved HTTPS egress through NAT, a Transit Gateway, public routing, or a reachable proxy."
  type        = list(string)
  default     = []
}

variable "browser_worker_security_group_ids" {
  description = "Optional existing security groups for browser tasks; defaults to the Terraform-managed HTTPS-egress group"
  type        = list(string)
  default     = []
}

variable "browser_worker_assign_public_ip" {
  description = "Assign public IPs to browser tasks. Keep false when private subnets have NAT, Transit Gateway egress, or a reachable approved proxy."
  type        = bool
  default     = false
}

variable "browser_worker_proxy_secret_arn" {
  description = "Optional Secrets Manager ARN containing an approved proxy URL or JSON {server/url,username,password}"
  type        = string
  default     = ""
}

variable "browser_worker_max_attempts" {
  description = "Persistent-browser attempts per WAF fallback job"
  type        = number
  default     = 3
}

variable "browser_worker_retry_delay_seconds" {
  description = "Delay between long-running browser attempts"
  type        = number
  default     = 20
}

variable "browser_worker_nav_timeout_ms" {
  description = "Chromium navigation timeout for each official URL"
  type        = number
  default     = 90000
}

variable "browser_worker_max_document_bytes" {
  description = "Maximum downloaded document size accepted by the worker"
  type        = number
  default     = 52428800
}

variable "browser_worker_visibility_timeout_seconds" {
  description = "SQS visibility timeout for one bounded browser job"
  type        = number
  default     = 1800
}

variable "browser_worker_max_agent_steps" {
  description = "Maximum LLM-directed Playwright actions per browser job"
  type        = number
  default     = 18
}

variable "browser_worker_max_contexts" {
  description = "Maximum live isolated per-domain browser contexts retained by the worker"
  type        = number
  default     = 8
}

variable "browser_worker_max_jobs_per_process" {
  description = "Jobs handled before Chromium exits cleanly and ECS restarts it to bound memory growth"
  type        = number
  default     = 100
}

variable "browser_worker_context_max_age_seconds" {
  description = "Maximum age of a live domain browser context before recycling"
  type        = number
  default     = 21600
}

variable "browser_worker_planner_model_id" {
  description = "Bedrock multimodal model for bounded browser navigation decisions"
  type        = string
  default     = "us.amazon.nova-2-lite-v1:0"
}

variable "browser_worker_verifier_model_id" {
  description = "Bedrock model for strict company and document-class verification"
  type        = string
  default     = "us.anthropic.claude-sonnet-5"
}

variable "browser_worker_verifier_fallback_model_id" {
  description = "Bedrock fallback verifier used when the primary model/profile rejects a request"
  type        = string
  default     = "us.amazon.nova-2-lite-v1:0"
}

variable "browser_worker_state_prefix" {
  description = "Encrypted S3 prefix for per-domain Playwright cookie and local-storage state"
  type        = string
  default     = "_browser-state"
}

variable "alb_ingress_cidrs" {
  description = "CIDR ranges allowed to reach the ALB on port 80"
  type        = list(string)
  default     = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
}

# ─── Existing resources the app talks to ─────────────────────────────────────
variable "reports_bucket" {
  description = "Existing S3 bucket for downloaded reports"
  type        = string
  default     = "edo-coanalyst-report-610639371721"
}

variable "provenance_table" {
  description = "Existing DynamoDB provenance table"
  type        = string
  default     = "edo-coanalyst-report-provenance"
}

variable "queries_table" {
  description = "DynamoDB web-queries table"
  type        = string
  default     = "reportiq-web-queries"
}

variable "runs_table" {
  description = "DynamoDB runs table"
  type        = string
  default     = "reportiq-runs"
}

variable "agent_runtime_arn" {
  description = "AgentCore runtime ARN to invoke"
  type        = string
  default     = "arn:aws:bedrock-agentcore:us-east-1:610639371721:runtime/edo_coanalyst_report-3dAfJRHyfY"
}

variable "agent_qualifier" {
  description = "AgentCore qualifier"
  type        = string
  default     = "DEFAULT"
}

variable "manage_dynamo_tables" {
  description = "If true, Terraform creates the queries+runs tables. If false, they must already exist."
  type        = bool
  default     = true
}

variable "create_vpc_endpoints" {
  description = "Create ECR/S3/logs VPC endpoints (needed if subnets have no NAT gateway). Set false if a NAT or the endpoints already exist."
  type        = bool
  default     = true
}

variable "hosted_zone_id" {
  description = "Route53 private hosted zone ID for novavoice.spglobal.com"
  type        = string
  default     = "Z0486311J00RNSG5XGBS"
}

variable "dns_name" {
  description = "Friendly DNS name for the app (must be in the hosted zone)"
  type        = string
  default     = "reportiq.novavoice.spglobal.com"
}
