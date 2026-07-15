data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  acct   = data.aws_caller_identity.current.account_id
  region = data.aws_region.current.region
  # Runtime names must be alphanumeric + underscore.
  runtime_name = replace(var.name, "-", "_")
  image_uri    = "${aws_ecr_repository.agent.repository_url}:${var.image_tag}"

  # Gateway MCP URL ("" when web search is disabled). The agent uses it if set,
  # else falls back to direct search.
  gateway_url = length(aws_bedrockagentcore_gateway.web_search) > 0 ? aws_bedrockagentcore_gateway.web_search[0].gateway_url : ""

  # Single source of truth for the runtime's environment variables. Used by BOTH
  # the aws_bedrockagentcore_agent_runtime resource (initial create) and the
  # null_resource that forces a new version on every image_tag/env change (the
  # provider does not reliably detect these as a diff). Keys here MUST match the
  # names the agent reads via os.environ.get(...).
  runtime_env = {
    REPORTS_BUCKET         = aws_s3_bucket.reports.id
    PROVENANCE_TABLE       = aws_dynamodb_table.provenance.name
    APP_REGION             = local.region
    WEB_SEARCH_MAX_RESULTS = tostring(var.web_search_max_results)
    BEST_MATCHES           = tostring(var.best_matches)
    LLM_MODEL_ID           = var.llm_model_id
    ENFORCE_SITE_DOMAIN    = tostring(var.enforce_site_domain)
    GATEWAY_URL            = local.gateway_url
    GOOGLE_API_KEY         = var.google_api_key
    GOOGLE_CX              = var.google_cx
    SERPER_API_KEY         = var.serper_api_key
    USE_BROWSER            = tostring(var.use_browser)
    BROWSER_IDENTIFIER     = var.browser_identifier
    CODE_VERSION           = var.image_tag
    GATEWAY_SEARCH_TOOL    = var.gateway_search_tool   
    GATEWAY_STRIP_SITE     = tostring(var.gateway_strip_site)  
    BROWSER_REGION         = "us-east-1"
    BROWSER_SESSION_TIMEOUT_SECONDS = "120"
    BROWSER_WAIT_UNTIL     = "domcontentloaded"
    BROWSER_SETTLE_MS      = "2500"
    BROWSER_VISION_MODEL_ID = "us.amazon.nova-2-lite-v1:0"
    BROWSER_MAX_VERIFY_CANDIDATES = "10"
    BROWSER_CLICK_TIMEOUT_MS = "10000"
    EDGAR_USER_AGENT="EDO-CoAnalyst/1.0 compliance-research askdevopscloud@spglobal.com"
    ENABLE_REGISTRY_TIER    = "true"
    EDGAR_MAX_REQ_PER_SEC   = "8"
    EDGAR_SUSTAINABILITY_FTS = "false"
    #SELECTION_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
  }
}
