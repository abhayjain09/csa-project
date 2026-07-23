# ---------------------------------------------------------------------------
# Naming / environment
# ---------------------------------------------------------------------------

variable "project_name" {
  description = "Short slug used to prefix resource names (lowercase, dashes)."
  type        = string
  default     = "pageindex-agent"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}$", var.project_name))
    error_message = "project_name must be lowercase, start with a letter, 2-31 chars, dashes only."
  }
}

variable "environment" {
  description = "Deployment environment (dev/stg/prd)."
  type        = string
  default     = "dev"
}

variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "additional_tags" {
  description = "Extra tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}

# ---------------------------------------------------------------------------
# IAM — existing role (optional)
# ---------------------------------------------------------------------------

variable "existing_role_arn" {
  description = <<-EOT
    ARN of a pre-existing IAM role to use as the AgentCore execution role.

    When set:
      - Terraform does NOT create a new IAM role or any inline policies.
      - The existing role is used directly in the AgentCore runtime resource.
      - You are responsible for ensuring the role has the required permissions
        (see iam.tf for the full policy set, or the README for a summary).

    When left empty (default):
      - Terraform creates a new role named <project_name>-<environment>-runtime
        with all required inline policies attached.

    Required permissions on the existing role:
      - ecr:GetAuthorizationToken (on *)
      - ecr:BatchGetImage, ecr:GetDownloadUrlForLayer,
        ecr:BatchCheckLayerAvailability (on the ECR repo ARN)
      - bedrock:Converse, bedrock:ConverseStream,
        bedrock:InvokeModel, bedrock:InvokeModelWithResponseStream
        (on the Bedrock model ARN)
      - s3:GetObject, s3:GetObjectVersion (on the input bucket/*)
      - s3:ListBucket (on the input bucket)
      - logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents,
        logs:DescribeLogStreams, logs:DescribeLogGroups
        (on arn:aws:logs:*:*:log-group:/aws/bedrock-agentcore/*)
      - cloudwatch:PutMetricData, xray:PutTraceSegments,
        xray:PutTelemetryRecords (on *)

    The role's trust policy must allow bedrock-agentcore.amazonaws.com
    to assume it. Example trust policy:
      {
        "Effect": "Allow",
        "Principal": { "Service": "bedrock-agentcore.amazonaws.com" },
        "Action": "sts:AssumeRole",
        "Condition": {
          "StringEquals": { "aws:SourceAccount": "<your-account-id>" },
          "ArnLike": {
            "aws:SourceArn": "arn:aws:bedrock-agentcore:<region>:<account>:runtime/*"
          }
        }
      }
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.existing_role_arn == "" || can(regex("^arn:aws[^:]*:iam::[0-9]{12}:role/.+$", var.existing_role_arn))
    error_message = "existing_role_arn must be empty or a valid IAM role ARN (arn:aws:iam::<account>:role/<name>)."
  }
}

# ---------------------------------------------------------------------------
# Container image
# ---------------------------------------------------------------------------

variable "image_tag" {
  description = <<-EOT
    Image tag to deploy to the runtime. Change this (via -var or a CI pipeline)
    when you push a new image so AgentCore fetches the new artifact. Using a
    mutable 'latest' tag will NOT trigger a runtime redeploy on its own — AWS
    caches by image URI including the tag string. Use content-derived tags
    (e.g. git SHA) in production.
  EOT
  type        = string
  default     = "latest"
}

# ---------------------------------------------------------------------------
# Input storage (pageindex + questionnaire)
# ---------------------------------------------------------------------------

variable "create_input_bucket" {
  description = "If true, create a dedicated S3 bucket for pageindex JSON and questionnaire MD. If false, provide existing_input_bucket_name."
  type        = bool
  default     = true
}

variable "existing_input_bucket_name" {
  description = "Name of a pre-existing S3 bucket holding pageindex/questionnaire assets. Ignored when create_input_bucket = true."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Bedrock model
# ---------------------------------------------------------------------------

variable "bedrock_model_id" {
  description = "Bedrock model ID the agent will invoke."
  type        = string
  default     = "amazon.nova-pro-v1:0"
}

# ---------------------------------------------------------------------------
# Agent runtime tunables (env vars forwarded to the container)
# ---------------------------------------------------------------------------

variable "tool_call_budget" {
  description = "Max tool calls per question (env AGENT_TOOL_BUDGET)."
  type        = number
  default     = 15
}

variable "max_page_span" {
  description = "Max pages per fetch_pages call (env AGENT_MAX_PAGE_SPAN)."
  type        = number
  default     = 15
}

variable "max_parallel_questions" {
  description = "Concurrency for question processing (env AGENT_MAX_PARALLEL). Keep at 1 to respect Bedrock TPS limits until measured."
  type        = number
  default     = 1
}

variable "staleness_warn_days" {
  description = "Warn if the pageindex is older than N days (env AGENT_STALENESS_DAYS)."
  type        = number
  default     = 30
}

# ---------------------------------------------------------------------------
# Runtime lifecycle
# ---------------------------------------------------------------------------

variable "idle_session_timeout_seconds" {
  description = "How long (s) an idle session lives before AgentCore reaps it. Range 60–3600. Lower = better cost control in dev."
  type        = number
  default     = 900 # 15 min — AgentCore default
}

variable "max_session_lifetime_seconds" {
  description = "Absolute max lifetime (s) of one session. Range 60–28800 (8 h)."
  type        = number
  default     = 3600
}

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

variable "network_mode" {
  description = "PUBLIC gives the runtime internet egress. VPC keeps it inside your VPC — requires vpc_subnet_ids and vpc_security_group_ids."
  type        = string
  default     = "PUBLIC"

  validation {
    condition     = contains(["PUBLIC", "VPC"], var.network_mode)
    error_message = "network_mode must be PUBLIC or VPC."
  }
}

variable "vpc_subnet_ids" {
  description = "Subnet IDs when network_mode = VPC."
  type        = list(string)
  default     = []
}

variable "vpc_security_group_ids" {
  description = "Security group IDs when network_mode = VPC."
  type        = list(string)
  default     = []
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

variable "log_retention_days" {
  description = "CloudWatch retention for runtime logs."
  type        = number
  default     = 30
}
