# Report IQ document retrieval

This runtime retrieves one validated corporate document for each structured
request. It is registry-first: it does not fan out a list of loose search
phrases and it never stores an unverified best guess.

## Retrieval policy

```text
company + document request
  -> Tier 0: official filing registry (SEC EDGAR, Companies House)
  -> Tier 1: verified company site paths and sitemap links
  -> Tier 2: optional site-scoped Google PSE or Brave search
  -> deterministic PDF/HTML validation + optional Bedrock confirmation
  -> exactly one winner written to S3 and DynamoDB
```

Each tier exits as soon as it finds a positively validated document. A URL may
only be accepted if it is from an official registry, an official company domain,
or an explicitly trusted document CDN linked from an official company page.

## Input contract

Use one item in `requests` per desired document. Do not submit 23 variations of
the same query: that was the reason the previous runtime stored several reports.

```json
{
  "company": {
    "legal_name": "PACCAR Inc",
    "aliases": ["PACCAR"],
    "cik": "0000753362",
    "country": "US",
    "official_domains": ["paccar.com"],
    "trusted_document_hosts": ["q4cdn.com"]
  },
  "requests": [
    {"id": "annual-2024", "document_type": "annual_report", "year": 2024},
    {"id": "conduct", "document_type": "code_of_conduct"}
  ]
}
```

Supported `document_type` values are defined in
[document_types.json](agent/config/document_types.json): `annual_report`,
`proxy_statement`, `sustainability_report`, `code_of_conduct`,
`anti_bribery_policy`, `whistleblowing_policy`, `tax_strategy`, and
`supplier_code_of_conduct`.

The company object must include `legal_name` and either `official_domains`, a
SEC `cik`, or a UK `companies_house_number`. Adding `cik` is strongly recommended
for US annual reports and proxies. It turns EDGAR into the first deterministic
source instead of relying on search.

## Validation and storage

Before storage a candidate must satisfy all applicable gates:

- Supported PDF or HTML document content.
- Exact document type, not a neighbouring report or policy.
- Correct company identity from text, registry identity, or official-domain provenance.
- Exact requested year when `year` is provided.
- A deterministic score of at least 80.
- Bedrock confirmation when `require_llm_validation = true`.

The runtime stores only the winning document under:

```text
<company>/<document_type>/<year-or-undated>/<sha12>-<filename>
```

DynamoDB receives source tier, registry metadata, validation score, reasons,
and the S3 key. Rejected URLs remain only in the invocation diagnostics.

## Configuration

Start from `terraform.tfvars.example`. Production needs:

```hcl
llm_model_id             = "us.amazon.nova-2-lite-v1:0"
require_llm_validation   = true
sec_user_agent           = "Your Organisation ops@example.com"
```

`SEC_USER_AGENT` must name an organisation and monitored email address. EDGAR is
queried at eight requests per second or less. The runtime uses the SEC submissions
API and Archives documents, not fragile SEC HTML search.

Set `companies_house_api_key` to enable UK statutory accounts. Set both
`google_api_key` and `google_cx`, or `brave_search_api_key`, only if you need
site-scoped web search for website-only policy classes. Search results are still
hard-filtered to `official_domains`.

The old AgentCore managed web-search gateway is disabled by default because its
backend does not reliably honour `site:` scoping. It remains optional Terraform
infrastructure for other broad-discovery uses, but this retrieval runtime does not
use it.

## Deploy and invoke

```bash
terraform init
terraform apply -target=aws_ecr_repository.agent
./scripts/build_and_push.sh "$(aws sts get-caller-identity --query Account --output text)" \
  us-east-1 "$(terraform output -raw ecr_repository_url)" v2
terraform apply -var image_tag=v2

python scripts/invoke_local.py "$(terraform output -raw agent_runtime_arn)" \
  scripts/payload.example.json --region=us-east-1
```

The runtime has public egress because it must call official registries and company
sites. Keep S3, DynamoDB, and Bedrock access inside AWS as configured by Terraform.

## Browser and country adapters

The original deep browser crawler was deliberately removed from the synchronous
runtime. It was selecting and downloading documents before there was strong source
identity. For JavaScript-only or bot-protected website policies, add a separate
Lambda/Fargate browser worker as a future Tier 3. Its output must use the same
candidate contract and validator in `agent.py`; it must not write directly to S3.

SEC EDGAR and Companies House are implemented now. India BSE/NSE and EU OAM/ESEF
should be added as separate registry adapters once their issuer identifiers and
API behaviour are verified in your AWS account. Do not substitute an unscoped web
search for those adapters.
