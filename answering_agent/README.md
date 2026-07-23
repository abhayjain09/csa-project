# PageIndex ReAct Agent

Vectorless RAG agent that answers ESG questionnaires by traversing a hierarchical
`pageindex` JSON over PDF disclosures. Runs on AWS Bedrock AgentCore Runtime
inside an ARM64 container.

## Layout

```
agent/                  container build context (Docker builds from here)
├── Dockerfile          ARM64 Python 3.12 + FastAPI + uvicorn
├── .dockerignore
├── requirements.txt
├── runtime_entrypoint.py  FastAPI app — /invocations POST, /ping GET
├── pipeline.py         orchestration (preflight, per-question loop, aggregation)
├── config.py           env-var-driven config
├── agent/              ReAct loop, session state, Bedrock Converse wrapper
├── pageindex/          pure read-only navigation over the tree
├── pdf/                S3 fetch + pypdf per-page extraction
├── tools/              7 tool defs (definitions, handlers, dispatcher)
├── prompts/            MD parser + prompt assembler + constant instructions
├── validation/         preflight, output schema, confidence sanity
├── models/schemas.py   Pydantic types (single source of truth)
├── utils/logging.py    JSON structured logging
├── smoke_test.py       exercises pure-logic paths with real pydantic
└── offline_smoke_test.py  same, stubs pydantic — 36 checks, all passing

infra/                  Terraform (deploys ECR + IAM + AgentCore runtime)
├── versions.tf
├── variables.tf
├── locals.tf
├── ecr.tf              ECR repository + lifecycle policy
├── iam.tf              execution role (Bedrock + S3 + ECR + logs)
├── s3.tf               optional input bucket (pageindex + questionnaire)
├── runtime.tf          aws_bedrockagentcore_agent_runtime + endpoint
├── logs.tf
├── outputs.tf
├── terraform.tfvars.example
└── README.md           deploy sequence, invocation examples, troubleshooting
```

## Architecture

```
Client (SigV4 or Cognito JWT)
       │
       ▼
InvokeAgentRuntime API
       │
       ▼
AgentCore Runtime endpoint (session-isolated container)
       │
       ▼
FastAPI /invocations  →  pipeline.run_pipeline
                              │
                              ├── load pageindex (S3 or inline)
                              ├── parse questionnaire MD (6 sections)
                              ├── preflight (freshness, S3 access, dup IDs)
                              │
                              └── per QUESTION_BLOCK:
                                     ├── Session + assembled prompt
                                     └── ReAct loop (Bedrock Converse + 7 tools)
                                            list_documents · get_outline · expand_node
                                            fetch_pages · keyword_scan · record_citation
                                            submit_answer
```

## Deploy

Full step-by-step in `infra/README.md`. TL;DR:

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # edit vars
terraform init
terraform apply -target=aws_ecr_repository.agent -target=aws_iam_role.runtime -target=aws_s3_bucket.input

ECR_URL=$(terraform output -raw ecr_repository_url)
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "$ECR_URL"
docker buildx build --platform linux/arm64 --tag "$ECR_URL:latest" --push ../agent

terraform apply   # creates the runtime + endpoint
```

## Integrity properties

Baked into the ReAct loop, not left to model discipline:

- **Citations reference pages the agent actually fetched.** The `record_citation`
  handler checks `session.fetched_ranges` before accepting.
- **Quoted spans are verbatim substrings** of fetched page text (whitespace-normalized).
- **`node_path` and `s3_uri`** on citations are attached by the handler, not the model.
- **Confidence is validated post-hoc** against evidence signals — the model
  can't self-declare `high` with zero citations.

## Testing

- `offline_smoke_test.py` — 36 checks, no deps needed, exercises schemas +
  navigator + prompt loader + prompt assembler. Run: `python offline_smoke_test.py`.
- `smoke_test.py` — same checks but requires real pydantic; use in CI.
- Integration tests (real Bedrock + S3) run against a deployed runtime via
  `InvokeAgentRuntime` — see `infra/README.md` for the invocation payload.
