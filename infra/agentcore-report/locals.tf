data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  acct   = data.aws_caller_identity.current.account_id
  region = data.aws_region.current.region
  # Runtime names must be alphanumeric + underscore.
  runtime_name     = replace(var.name, "-", "_")
  image_uri        = "${aws_ecr_repository.agent.repository_url}:${var.image_tag}"
  worker_image_uri = "${aws_ecr_repository.browser_worker.repository_url}:${var.image_tag}"

  # Single source of truth for the runtime's environment variables. Used by BOTH
  # the aws_bedrockagentcore_agent_runtime resource (initial create) and the
  # null_resource that forces a new version on every image_tag/env change (the
  # provider does not reliably detect these as a diff). Keys here MUST match the
  # names the agent reads via os.environ.get(...).
  runtime_env = {
    REPORTS_BUCKET                  = aws_s3_bucket.reports.id
    PROVENANCE_TABLE                = aws_dynamodb_table.provenance.name
    APP_REGION                      = local.region
    WEB_SEARCH_MAX_RESULTS          = tostring(var.web_search_max_results)
    LLM_MODEL_ID                    = var.llm_model_id
    REQUIRE_LLM_VALIDATION          = tostring(var.require_llm_validation)
    SEC_USER_AGENT                  = var.sec_user_agent
    COMPANIES_HOUSE_API_KEY         = var.companies_house_api_key
    GOOGLE_API_KEY                  = var.google_api_key
    GOOGLE_CX                       = var.google_cx
    BRAVE_SEARCH_API_KEY            = var.brave_search_api_key
    USE_BROWSER                     = tostring(var.use_browser)
    BROWSER_REGION                  = var.browser_region
    BROWSER_IDENTIFIER              = var.browser_identifier
    BROWSER_SESSION_TIMEOUT_SECONDS = "180"
    BROWSER_MAX_PAGES               = "12"
    BROWSER_MAX_SECONDS             = "120"
    BROWSER_CLICK_TIMEOUT_MS        = "12000"
    FARGATE_BROWSER_QUEUE_URL       = var.enable_fargate_browser_worker ? aws_sqs_queue.browser_jobs[0].id : ""
    MAX_CANDIDATES_PER_TIER         = "8"
    SITEMAP_MAX_URLS                = "2000"
    FETCH_TIMEOUT_SECONDS           = "45"
    CODE_VERSION                    = var.image_tag
  }
}
