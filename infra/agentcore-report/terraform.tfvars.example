# Copy to terraform.tfvars (auto-loaded).

region    = "us-east-1"
image_tag = "v1"

# Managed Web Search tool (Gateway + web-search connector via AWS CLI).
enable_web_search      = true
web_search_max_results = 10

# Documents per query (1 = single best match per query).
best_matches = 1

# Enforce official domain only (strongly recommended — the managed search tool
# ignores site: so we filter ourselves).
enforce_site_domain = true

# LLM document relevance selector. Set to a Bedrock model id you have enabled
# in us-east-1 to reject wrong documents (e.g. political-policy for annual-report).
# NOTE: the first-gen Nova models (nova-micro/lite/pro v1:0) are now LEGACY/EOL and
# return "model has reached end of life" — do NOT use them. Live options:
#   "amazon.nova-2-lite-v1:0"                     (current gen, cheap, recommended)
#   "us.anthropic.claude-haiku-4-5-20251001"      (best judgement, ~$30/10k runs)
# Leave empty to skip the relevance check (then wrong docs are kept).
llm_model_id = "us.amazon.nova-2-lite-v1:0" #"amazon.nova-lite-v1:0"

# Optional literal Google search (honors site:). Leave empty to use managed/DDG.
# google_api_key = "AIza..."
# google_cx      = "0123abc..."

# AgentCore Browser tool — in-AWS headless Chromium, NO third-party data egress.
# RECOMMENDED when company policy forbids third-party search tools. Renders JS and
# finds deep PDF links the managed search tool misses. Slower per query (spins a
# browser session) but fully in-account. The crawl + managed tool remain fallback.
use_browser = true

# Serper.dev — only if third-party tools are permitted (they send queries off-AWS).
# Leave empty if company policy forbids third-party tools (use_browser instead).
# serper_api_key = "your-serper-key"
