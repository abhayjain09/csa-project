# AgentCore report-download agent

Deploy one AgentCore Runtime. Invoke it from your laptop with a `web_query` JSON;
it searches the official domain, downloads the report(s) to S3, records provenance
in DynamoDB, and **returns the S3 key(s)**.

## Project files
```
agent/
  agent.py            # the agent: search -> rank -> deep-crawl -> LLM gate -> S3 + DynamoDB
  Dockerfile          # ARM64 image; installs Playwright for the in-AWS Browser tool
  requirements.txt    # bedrock-agentcore, boto3, mcp, httpx, playwright
versions.tf           # required providers: aws >=6.32, null, local
providers.tf          # aws provider + mandatory default_tags
variables.tf          # all input vars (region, image_tag, search/browser toggles, ...)
locals.tf             # runtime_env map = single source of truth for runtime env vars
main.tf               # ECR, S3, DynamoDB, IAM, CloudWatch, runtime + runtime_update null_resource
gateway.tf            # AgentCore Gateway + managed Web Search target (via AWS CLI)
outputs.tf            # runtime ARN, bucket, table, gateway URL, region, invoke example
terraform.tfvars.example
scripts/
  deploy.sh           # one-command: build + push + terraform apply
  build_and_push.sh   # build ARM64 image, push to ECR
  invoke_local.py     # invoke the runtime from your laptop, print S3 keys
  payload.example.json
  payload.xylem.json  # example: Xylem policies/reports
README.md
```

## Resources this Terraform creates
ECR repo · S3 reports bucket (versioned, KMS, private) · DynamoDB provenance table
· IAM execution role · CloudWatch log group · **AgentCore Gateway + Web Search
target** · **AgentCore Runtime (+ auto DEFAULT endpoint)** · **AgentCore Browser
tool** (in-AWS headless Chromium, no third-party egress).
All six mandatory tags applied via `default_tags`.


## Prerequisites
- Terraform >= 1.9, AWS provider >= 6.32 (AgentCore needs 6.18+).
- **AWS CLI v2** on the machine running `terraform apply` (used to create the
  Web Search target — see below).
- Docker with `buildx` (the runtime image is **ARM64/Graviton**).
- Region: **us-east-1** — the Web Search tool is only in us-east-1 today.
- Bedrock model access enabled in the console (one-time) if you later add model calls.

## Deploy (three commands)
First pin the region (cleaner than `-var` on every call):
```bash
cp terraform.tfvars.example terraform.tfvars   # already set to us-east-1
```
Then:
```bash
# 1. Create the ECR repo (and the rest of the non-runtime infra) first
terraform init
terraform apply -target=aws_ecr_repository.agent

# 2. Build + push the ARM64 image into that repo
./scripts/build_and_push.sh "$(aws sts get-caller-identity --query Account --output text)" \
    us-east-1 "$(terraform output -raw ecr_repository_url)" v1

# 3. Create everything else: Gateway + Web Search target + runtime + endpoint
terraform apply
```
> Re-deploying new code later — one command:
> ```bash
> ./scripts/deploy.sh v2
> ```
> This builds+pushes the image and runs `terraform apply -var image_tag=v2`. The
> `null_resource.runtime_update` then calls `update-agent-runtime` to create a NEW
> runtime version from the new image (and current env), which the auto DEFAULT
> endpoint tracks. You can also do it by hand: rebuild/push a new tag, then
> `terraform apply -var image_tag=v2`.

### Why the runtime update is a null_resource (provider gap)
The `aws_bedrockagentcore_agent_runtime` provider does **not** detect image-tag or
environment-variable changes as a diff — a second `terraform apply` reports "no
changes" and the runtime stays pinned to its first version, so new code never goes
live. To work around this, the runtime resource sets `lifecycle.ignore_changes` on
the artifact + env (it only handles the INITIAL create), and
`null_resource.runtime_update` runs `aws bedrock-agentcore-control
update-agent-runtime` on every `image_tag`/env change. That call creates a fresh
immutable version; the auto-created DEFAULT endpoint always tracks the latest
version, so `--qualifier DEFAULT` invokes get the new build. The env is written to
a git-ignored `.runtime-env.json` (sensitive) and passed via `--environment-variables file://`.

## Invoke from your laptop
Your IAM identity needs `bedrock-agentcore:InvokeAgentRuntime` on the runtime ARN.

**Option A — helper script (prints the S3 keys):**
```bash
pip install boto3
python scripts/invoke_local.py "$(terraform output -raw agent_runtime_arn)" \
    scripts/payload.example.json --region=us-east-1
```

**Option B — raw AWS CLI:**
```bash
aws bedrock-agentcore invoke-agent-runtime \
  --region us-east-1 \
  --agent-runtime-arn "$(terraform output -raw agent_runtime_arn)" \
  --qualifier DEFAULT \
  --payload fileb://scripts/payload.example.json \
  out.json && cat out.json
```

`payload.example.json` is your exact input:
```json
{
  "web_query1": "site: https://www.paccar.com/ Anti-Bribery & Anti-Corruption Policy ",
  "web_query2": " site: https://www.paccar.com/ Anti-Bribery & Anti-Corruption Policy"
}
```

## What you get back
```json
{
  "run_id": "a1b2c3d4",
  "company": "paccar",
  "domain": "paccar.com",
  "bucket": "edo-coanalyst-report-<acct>",
  "count": 2,
  "downloaded": [
    {
      "status": "stored",
      "s3_key": "paccar/a1b2c3d4/code-of-conduct.pdf",
      "s3_uri": "s3://edo-coanalyst-report-<acct>/paccar/a1b2c3d4/code-of-conduct.pdf",
      "source_url": "https://www.paccar.com/.../code-of-conduct.pdf",
      "content_type": "application/pdf",
      "sha256": "…",
      "report": "PACCAR Code of Conduct"
    }
  ],
  "failures": []
}
```
The `s3_key` / `s3_uri` of each downloaded report is the answer you asked for.
The same rows are written to the DynamoDB provenance table.

## Latest annual-report behavior

An Annual Report is labeled by its completed fiscal year, not by the calendar
year in which the download runs. An undated Annual Report request therefore
targets `current year - 1`: a run in 2026 requests FY2025, and a run in 2027
requests FY2026. An explicitly requested historical year is never changed.

For filing classes, the rendered investor-relations path runs before a broad
corporate-site crawl. Static discovery is capped per page, preserves verification
capacity for the browser, and may accept a configured document CDN only when the
link originates on an official investor-relations page. The deployment defaults
are controlled by `LATEST_COMPLETED_FISCAL_YEAR_LAG`,
`DEEP_STATIC_MAX_DOC_CANDIDATES_PER_PAGE`, `BROWSER_RESERVED_VERIFIES`, and
`TRUSTED_DOCUMENT_CDN_DOMAINS` in `locals.tf`.

## How it handles your minimal JSON
No `company` field is needed — it's derived from the `site:` domain
(`paccar.com` → `paccar`). The agent keeps only on-domain results and, when a hit
is an HTML governance page, follows the same-domain **PDF** links on it (policies
are usually PDFs), so you get the actual document, not just the landing page.

## Web search (managed, zero egress)
The agent uses AWS's **managed Web Search tool** — Amazon's own web index, queries
never leave AWS, results come back with snippets, URLs, titles, and dates. The
agent connects to the Gateway over MCP (SigV4-signed), discovers the tool via
`tools/list`, and calls it. If the gateway is disabled or a call fails, it falls
back to direct search so a run still returns documents.

### Why part of this is AWS CLI, not pure Terraform
The Web Search tool is a built-in Gateway **connector** (`connectorId: "web-search"`).
The `hashicorp/aws` provider supports the **Gateway** resource but does **not** yet
expose the connector **target** type. So:
- the Gateway + its service role are plain Terraform, and
- the web-search **target** is created by the AWS CLI from a `null_resource`
  during `terraform apply` (idempotent; a "already exists" conflict is treated as
  success; removed on `terraform destroy`).

This is the standard way to bridge a provider gap without leaving `terraform apply`.
When the provider adds a connector target resource, swap the `null_resource` in
`gateway.tf` for the native resource — nothing else changes.

To deploy without web search (agent falls back to direct search):
```bash
terraform apply -var enable_web_search=false
```

> The AgentCore starter-toolkit CLI (`agentcore add gateway-target`) supports
> lambda/openapi/smithy/mcp-server targets, but the **web-search connector** is
> only documented via the AWS CLI / boto3 today — which is what this stack uses.

> Not to be confused with the AgentCore **Browser tool** (`aws_bedrockagentcore_browser`)
> — that's a headless browser for live-site navigation, useful later for a
> verifier agent, not for search.

## Honesty / things to verify
- `aws_bedrockagentcore_*` resources are new. This stack uses the verified schema
  (`agent_runtime_name`, endpoint via `agent_runtime_id`, gateway connector target
  via the documented CLI shape).
- If `apply` rejects `search_type = "SEMANTIC"` on the gateway, change it to
  `"DEFAULT"` in `gateway.tf`.
- `count: 0` from a run usually means the site exposes no linked document for that
  query — check `failures`, or refine the terms after `site:`.
- Logs: `/aws/bedrock-agentcore/<name>` in CloudWatch.
