# PageIndex ReAct Answering Agent — Complete System Documentation

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture](#2-architecture)
3. [How the PageIndex Works](#3-how-the-pageindex-works)
4. [How Prompts are Assembled](#4-how-prompts-are-assembled)
5. [How Questionnaires are Created and Parsed](#5-how-questionnaires-are-created-and-parsed)
6. [The ReAct Loop — Step by Step](#6-the-react-loop--step-by-step)
7. [Tool Descriptions](#7-tool-descriptions)
8. [Data Flow — End to End](#8-data-flow--end-to-end)
9. [Code Structure](#9-code-structure)
10. [Infrastructure](#10-infrastructure)
11. [Deployment](#11-deployment)
12. [Flask API Integration](#12-flask-api-integration)
13. [Frontend Dashboard](#13-frontend-dashboard)
14. [DynamoDB Schema](#14-dynamodb-schema)
15. [Configuration Reference](#15-configuration-reference)
16. [Known Issues and Fixes](#16-known-issues-and-fixes)

---

## 1. System Overview

The PageIndex ReAct Answering Agent is a vectorless RAG (Retrieval Augmented Generation) 
system that answers ESG questionnaires by navigating a hierarchical index of PDF disclosure 
documents. 

Unlike traditional RAG systems it does not use embeddings or vector search. Instead it uses 
a pre-built hierarchical JSON index (the pageIndex) that describes the structure of each PDF 
document — its sections, subsections, titles, summaries, and page ranges. The model navigates 
this index using tools, reads only the relevant pages, and produces cited answers.

### Key Properties

- **Vectorless** — no embeddings, no vector database, pure hierarchical navigation
- **Evidence-based** — every answer must be backed by a verbatim citation from a fetched page
- **Integrity-enforced** — citations are validated server-side, not trusted from the model
- **Session-isolated** — each question gets its own independent ReAct session
- **Auditable** — full tool call trace, citations with page numbers and quoted spans

---

## 2. Architecture

```
Caller (app.py or invoke.py)
        │
        │  boto3.invoke_agent_runtime()
        ▼
AgentCore Runtime (ARM64 container, session-isolated)
        │
        │  POST /invocations
        ▼
BedrockAgentCoreApp → @app.entrypoint → invoke()
        │
        ▼
pipeline.run_pipeline()
        │
        ├── load_pageindex()          S3 or inline dict → PageIndex object
        ├── load_questionnaire()      S3 or inline string → ParsedQuestionnaire
        ├── parse_question_set()      Parses --- QUESTION_BLOCK section
        ├── run_preflight()           Freshness, S3 access, duplicate ID checks
        ├── build_pageindex_summary() Short text summary for the prompt
        │
        └── for each QuestionBlock (sequential or parallel):
                │
                ├── assemble_prompt()     Builds system + user prompt
                └── run_react_loop()      ReAct loop with Bedrock Converse API
                        │
                        ├── list_documents    → dict walk (in memory)
                        ├── get_outline       → dict walk (in memory)
                        ├── expand_node       → dict walk (in memory)
                        ├── keyword_scan      → string match (in memory)
                        ├── fetch_pages       → S3 GetObject → pypdf
                        ├── record_citation   → validates quote in memory
                        └── submit_answer     → validates cited IDs, terminates
```

---

## 3. How the PageIndex Works

### What it is

The pageIndex is a JSON file stored in S3 that describes the structure of all PDF documents 
for a company. It is built by a separate pageIndex building agent and stored at:

```
s3://bucket/company-slug/company-slug_pageindex.json
```

### Structure

```json
{
  "company": "First Majestic Silver Corp.",
  "company_slug": "first-majestic",
  "bucket": "edo-coanalyst-report-610639371721",
  "model": "amazon.nova-lite-v1:0",
  "updated_at": "2026-07-09T10:32:15+00:00",
  "documents": [
    {
      "doc_name": "first-majestic-2024-sustainability.pdf",
      "structure": [
        {
          "title": "Water Management",
          "node_id": "0002",
          "start_index": 20,
          "end_index": 35,
          "summary": "Water withdrawal, discharge and consumption metrics.",
          "nodes": [
            {
              "title": "Water Withdrawal by Source",
              "node_id": "0003",
              "start_index": 22,
              "end_index": 24,
              "summary": "Breakdown by surface, ground and municipal sources.",
              "nodes": []
            }
          ]
        }
      ],
      "_meta": {
        "s3_key": "first-majestic/first-majestic-2024-sustainability.pdf",
        "s3_uri": "s3://bucket/first-majestic/first-majestic-2024-sustainability.pdf",
        "indexed_at": "2026-07-09T10:32:15+00:00"
      }
    }
  ]
}
```

### Required fields

| Field | Required | Notes |
|---|---|---|
| `company` | yes | Display name |
| `company_slug` | yes | Lowercase slug |
| `bucket` | yes | S3 bucket name |
| `updated_at` | yes | ISO 8601 timestamp |
| `documents` | yes | At least 1 document |
| `model` | no | Optional |
| `doc_name` | yes | Must be unique per document |
| `_meta.s3_uri` | yes | Must start with `s3://` |
| `structure[].title` | yes | Section title |
| `structure[].node_id` | yes | Unique within document |
| `structure[].start_index` | yes | 1-indexed page number |
| `structure[].end_index` | yes | Must be >= start_index |
| `structure[].summary` | yes | Can be empty string |
| `structure[].nodes` | yes | Must be present, `[]` if no children |

### How the agent uses it

The pageIndex is loaded once into memory at the start of each invocation. It is never sent 
to the model. Instead the model navigates it via tools:

```
Agent sees in prompt:    "Documents: first-majestic-2024-sustainability.pdf..."
                                    (one line per document, top section titles only)

Agent calls tools:       list_documents()     → Python iterates index.documents
                         get_outline()        → Python walks doc.structure tree
                         expand_node()        → Python finds node by node_id
                         keyword_scan()       → Python does substring match on titles+summaries

Agent only reads PDFs:   fetch_pages()        → S3 GetObject → pypdf text extraction
                         (only when a section looks relevant from the outline)
```

The summary field on each node is the primary navigation signal — the model reads summaries 
to decide which sections are worth drilling into before committing to a fetch_pages call.

---

## 4. How Prompts are Assembled

Each question gets its own isolated prompt. The assembler combines content from four sources:

### System prompt (stable reference material)

```
# System Directive
[from <system_directive> section of the MD file]
You are an ESG disclosure analyst...

# Traversal Instructions
[from agent/prompts/traversal_instructions.md — baked into container image]
7-step procedure: Orient → Survey → Drill → Read → Cite → Backtrack → Terminate
How to use metric_def, counts_as, does_not_count, fallback_rule
Budget rules, anti-patterns to avoid

# Question Block — Field Usage
[from agent/prompts/field_usage_note.md — baked into container image]
How to interpret each of the 6 question fields

# PageIndex Orientation
[auto-built from the loaded PageIndex object]
Company: First Majestic Silver Corp.
PageIndex last updated: 2026-07-09
Documents (3):
  - first-majestic-2024-sustainability.pdf — top sections: Water, Environment...
  - first-majestic-2024-annual-report.pdf — top sections: Operations, Financial...
```

### User prompt (question-specific)

```
# Question Set Context
[instructional text from <question_set> section ABOVE the --- QUESTION_BLOCK delimiter]
For each question, gather evidence and produce a structured answer...

# Current Question
[one QuestionBlock at a time — never all questions]
<question_block>
  id: Q1
  label: Total water withdrawal FY2024 (megaliters)
  metric_def: Total volume of freshwater withdrawn...
  counts_as: Surface water, groundwater, municipal, rainwater
  does_not_count: Recycled water; seawater
  fallback_rule: If not disclosed return null with insufficient_evidence
</question_block>

# Output Schema
[from <output_schema> section of the MD file]
{"type": "object", "properties": {"value": ..., "label": ...}}

# Example
[from <example> section of the MD file]
{"id": "Q1", "label": "...", "value": "12345", "unit": "megaliters"}

# Confidence Scoring
[from <confidence_scoring> section of the MD file]
high / medium / low / insufficient_evidence definitions

Begin. Use the tools to gather evidence, then call submit_answer.
```

### Key design decisions

- The `--- QUESTION_BLOCK` content is stripped from the prompt — the model only sees the 
  one question it is currently answering, never the full question list
- Traversal instructions and field usage notes are constant markdown files baked into the 
  container image — they do not change per invocation
- The pageIndex summary is the only part of the prompt that changes per company

---

## 5. How Questionnaires are Created and Parsed

### MD file structure

Each questionnaire is a single Markdown file stored in S3 under the `questionnaires/` prefix:

```
s3://bucket/questionnaires/code_of_conduct.md
s3://bucket/questionnaires/water_metrics.md
s3://bucket/questionnaires/tax_governance.md
```

Each file has two logical sections:

**Section A — Prompt instructions** (six `<tag>` blocks read by `prompt_loader.py`):

```markdown
<system_directive>
You are an ESG disclosure analyst. Answer strictly from the provided PDFs.
</system_directive>

<question_set>
For each question below, gather evidence and produce a structured answer.

--- QUESTION_BLOCK
id: Q1
label: Total water withdrawal FY2024 (megaliters)
metric_def: Total volume of freshwater withdrawn across all sites.
counts_as: Surface water, groundwater, municipal, rainwater.
does_not_count: Recycled water reused on-site; seawater.
fallback_rule: If not disclosed return null with insufficient_evidence.

id: Q2
label: ...
...
</question_set>

<output_schema>
```json
{"type": "object", "properties": {"value": {"type": "string"}, "label": {"type": "string"}}}
```
</output_schema>

<example>
{"id": "Q1", "label": "...", "value": "12345", "unit": "megaliters"}
</example>

<confidence_scoring>
high — primary source, exact match
medium — primary source, some interpretation
low — secondary source
insufficient_evidence — fallback invoked
</confidence_scoring>

<pre_flight_validation>
Confirm pageindex is fresh and all docs accessible.
</pre_flight_validation>
```

**Section B — Question blocks** (inside `<question_set>`, after `--- QUESTION_BLOCK`):

```
--- QUESTION_BLOCK
id: Q1
label: Total water withdrawal FY2024 (megaliters)
metric_def: ...
counts_as: ...
does_not_count: ...
fallback_rule: ...

id: Q2
...
```

### Parsing flow

**Step 1 — `prompt_loader.py` extracts six sections:**

```python
parsed_md = parse_questionnaire_md(md_text)
# Results in:
# parsed_md.system_directive       → "You are an ESG analyst..."
# parsed_md.question_set_wrapper   → full <question_set> content including QUESTION_BLOCK
# parsed_md.output_schema          → JSON schema string
# parsed_md.example                → example answer
# parsed_md.confidence_scoring     → confidence level definitions
# parsed_md.pre_flight_validation  → preflight rules
```

**Step 2 — `parse_question_set.py` extracts questions:**

```python
questions = parse_question_set_from_text(parsed_md.question_set_wrapper)
# Finds --- QUESTION_BLOCK delimiter
# Parses everything after it into QuestionBlock dicts
# Each block starts at a line beginning with "id:"
# Multi-line values are concatenated with spaces
# Results in:
# [
#   {"id": "Q1", "label": "...", "metric_def": "...", ...},
#   {"id": "Q2", ...},
# ]
```

**Step 3 — `prompt_assembler.py` strips questions from wrapper:**

```python
wrapper_instructions = _extract_wrapper_instructions(parsed_md.question_set_wrapper)
# Returns only text ABOVE the --- QUESTION_BLOCK delimiter
# "For each question below, gather evidence..."
# This goes into the prompt — not the raw question blocks
```

### The four question fields and how the agent uses them

| Field | Purpose | How agent uses it |
|---|---|---|
| `metric_def` | What the metric fundamentally is | Primary search anchor — match against node titles/summaries |
| `counts_as` | Inclusions | Broaden search — sections matching these also qualify |
| `does_not_count` | Exclusions | Deprioritize — any answer matching these is invalid |
| `fallback_rule` | Terminal decision | Only apply after exhausting reasonable search |

---

## 6. The ReAct Loop — Step by Step

The ReAct (Reasoning + Acting) loop is the core of the agent. It runs one iteration per 
Bedrock Converse API call.

### Loop flow

```
1. Send system prompt + user prompt + tool definitions to Bedrock Converse API

2. Model responds with either:
   a. tool_use blocks  → dispatch each tool, collect results
   b. text + end_turn  → nudge model to call submit_answer
   c. text + max_tokens → nudge model to submit with current evidence
   d. other stop reason → log warning, break loop

3. Append tool results to message history

4. Check nudges:
   - 80% budget used → warn model to converge
   - budget exhausted → force submit_answer on next turn
   - 2 consecutive empty fetches → suggest backtracking

5. If submit_answer was called → exit loop
   Otherwise → go to step 1

6. Post-loop validation:
   - output schema check
   - confidence sanity check (citation count, doc diversity, spec keywords)
```

### Budget and nudges

- Default tool call budget: 15 per question (configurable via `AGENT_TOOL_BUDGET`)
- At 80% (12 calls): `NUDGE_BUDGET_80` injected alongside next tool result
- At 100% (15 calls): `NUDGE_BUDGET_EXHAUSTED` forces submit_answer
- After 2 consecutive empty fetches: `NUDGE_DIMINISHING_RETURNS`
- Hard iteration cap: 50 (safety valve)

### Integrity checks (server-side, not trusted from model)

- `record_citation` validates pages were actually fetched before accepting
- Quoted spans must be verbatim substrings of fetched text (whitespace normalised)
- `submit_answer` validates all cited IDs exist in session
- Confidence is recalculated post-hoc from evidence signals

---

## 7. Tool Descriptions

### `list_documents()`
Returns catalog of all documents with top-level section titles and summaries. Called first 
for every question to understand what documents are available.

### `get_outline(doc_name, depth=1)`
Returns flat list of sections to depth N. Each entry has: node_id, title, summary, 
page_start, page_end, depth, path. Used to survey a document before drilling in.

### `expand_node(doc_name, node_id, depth=1)`
Returns children of a specific node. Used to drill into a promising section without 
loading the full tree.

### `keyword_scan(doc_name, terms)`
Case-insensitive substring match on titles and summaries across all nodes. Title hits 
weighted 2x over summary hits. Returns up to 20 ranked results.

### `fetch_pages(doc_name, page_start, page_end)`
Fetches actual PDF page text via S3 GetObject + pypdf. Maximum 15 pages per call. 
Returns text with `[p.N]` markers. Detects scanned/image PDFs and warns.

### `record_citation(doc_name, page_start, page_end, quoted_span)`
Records evidence. Validates:
- Pages were fetched in this session
- quoted_span is a verbatim substring of fetched text (whitespace normalised)
- quoted_span is under 300 characters
Returns citation_id (C001, C002...) for use in submit_answer.

### `submit_answer(answer, cited_ids, confidence, reasoning)`
Terminal tool. Validates:
- answer is a JSON object
- cited_ids all resolve to recorded citations
- confidence is one of: high, medium, low, insufficient_evidence
- If confidence != insufficient_evidence, at least one citation required
Exits the ReAct loop.

---

## 8. Data Flow — End to End

### Via invoke.py (direct test)

```
EC2 instance
    └── python3 scripts/invoke.py --endpoint-arn ... --payload payload.json
            │
            └── boto3.invoke_agent_runtime(payload)
                        │
                        ▼
            AgentCore Runtime container
                        │
                        ├── fetch pageindex.json from S3
                        ├── fetch questionnaire.md from S3
                        ├── parse 8 questions from --- QUESTION_BLOCK
                        │
                        └── for each question:
                                ReAct loop → citations → answer
                        │
                        └── return RunResult JSON
                        │
            invoke.py prints result to terminal
```

### Via app.py (production flow)

```
Frontend
    └── POST /api/answering-agent/run {"company": "First Majestic"}
                │
                ▼
        app.py returns run_id immediately (202)
        Background thread starts
                │
                ├── lookup pageindex_s3_uri from pageindex-runs DynamoDB
                ├── list all .md files from S3 questionnaires/ prefix
                │
                └── for each MD file (parallel, ANSWERING_MD_CONCURRENCY=3):
                        │
                        └── invoke AgentCore runtime
                                    │
                                    ▼
                            container processes questions
                            returns RunResult JSON
                                    │
                        app.py receives RunResult
                        writes each question result to answering-results DynamoDB
                        updates answering-runs status (md_done += 1)
                │
        All MD files complete → status = complete

Frontend polls GET /api/answering-agent/runs/<run_id>
Frontend fetches GET /api/answering-agent/companies/first-majestic/code-of-conduct
Frontend displays Q&A table
```

---

## 9. Code Structure

```
agent/
├── runtime_entrypoint.py
│   BedrockAgentCoreApp server. Handles /invocations and /ping.
│   @app.entrypoint calls run_pipeline() in a background thread.
│   SDK automatically manages HealthyBusy state during execution.
│
├── pipeline.py
│   Main orchestration. Loads pageIndex, parses questionnaire, runs preflight,
│   processes questions (sequential or parallel), aggregates RunResult.
│   Derives md_file and category from questionnaire S3 URI.
│   Logs prompt size for each question.
│
├── config.py
│   All tunables as a frozen dataclass. Every field reads from AGENT_* env vars
│   with safe defaults. Key fields:
│   - model_id (AGENT_MODEL_ID)
│   - max_output_tokens (AGENT_MAX_OUTPUT_TOKENS) = 8096
│   - tool_call_budget_per_question (AGENT_TOOL_BUDGET) = 15
│   - max_parallel_questions (AGENT_MAX_PARALLEL) = 1
│   - max_page_span (AGENT_MAX_PAGE_SPAN) = 15
│   - staleness_warn_days (AGENT_STALENESS_DAYS) = 30
│
├── models/schemas.py
│   All Pydantic types. Single source of truth.
│   Key types:
│   - RuntimePayload: question_set optional, model_validator coerces S3Refs
│   - PageIndex / PageIndexDocument / PageIndexNode / PageIndexDocMeta
│   - QuestionBlock: 6 required fields, all non-empty validated
│   - Citation: includes s3_uri and node_path (attached by handler, not model)
│   - RunResult: includes company_slug, md_file, category
│   - ConfidenceBreakdown: model_reported vs computed_floor vs final
│
├── pageindex/
│   ├── loader.py
│   │   load_pageindex(source, s3) — accepts dict or S3Ref.
│   │   Fetches from S3 if S3Ref, parses JSON, validates with Pydantic.
│   │
│   └── navigator.py
│       Pure read-only functions over the PageIndex tree. No I/O.
│       - find_document(index, doc_name) — case-insensitive, clear error with available list
│       - render_outline(doc, max_depth) — flat list of OutlineEntry objects
│       - expand_subtree(doc, node_id, max_depth) — children of a node
│       - locate_node(doc, node_id) — finds node with ancestor chain
│       - node_path_from_pages(doc, page_start, page_end) — deepest containing node
│       - keyword_scan(doc, terms) — weighted substring search, title hits 2x
│       - build_pageindex_summary(index) — short text for prompt
│
├── pdf/
│   ├── s3_client.py
│   │   boto3 S3 wrapper. LRU byte cache (32 objects). Adaptive retries (4 attempts).
│   │   parse_s3_uri() splits s3://bucket/key into (bucket, key).
│   │
│   └── page_extractor.py
│       pypdf-based page text extraction. Per-PDF reader cache. Per-page text cache.
│       get_range() enforces MAX_SPAN=15. Detects likely_scanned (>80% pages <20 chars).
│       format_range() adds [p.N] markers and scanned PDF warning.
│
├── tools/
│   ├── definitions.py
│   │   7 tool schemas in Bedrock Converse toolSpec format.
│   │   All schemas use additionalProperties:false.
│   │   TERMINAL_TOOL = "submit_answer"
│   │
│   ├── handlers.py
│   │   One function per tool. Each takes (session, index, extractor, tool_input).
│   │   Returns ToolResult — never raises.
│   │   record_citation integrity checks:
│   │     1. Verifies pages were fetched in this session
│   │     2. Verifies quoted_span is verbatim substring (whitespace normalised)
│   │     3. Attaches node_path and s3_uri from in-memory index
│   │   submit_answer validation:
│   │     1. All cited_ids must resolve
│   │     2. Non-insufficient confidence requires at least one citation
│   │
│   └── dispatcher.py
│       Routes tool calls to handlers. Records ToolCallRecord in session.
│       Tracks consecutive_empty_fetches for diminishing-returns nudge.
│       Wraps in try/except so unexpected errors surface as ToolResult(ERROR).
│
├── agent/
│   ├── session.py
│   │   Per-question mutable state. Tracks: tool_calls, citations, fetched_ranges,
│   │   submitted_answer, consecutive_empty_fetches, schema_retry_used.
│   │   find_fetched_text() checks if a range (or superset) was fetched.
│   │   next_citation_id() generates C001, C002...
│   │
│   ├── bedrock_client.py
│   │   Thin Converse API wrapper. No temperature (deprecated for Sonnet).
│   │   toolChoice: {"auto": {}} — required for Sonnet to use tools.
│   │   max_output_tokens = 8096.
│   │   Retries up to 3x on ModelErrorException with 1s/2s/4s backoff.
│   │
│   └── react_loop.py
│       The main loop. Handles:
│       - end_turn without tools → nudge to submit
│       - max_tokens → nudge to submit with current evidence
│       - other stop reasons → log warning, break
│       Nudges: BUDGET_80, BUDGET_EXHAUSTED, DIMINISHING_RETURNS
│       Forced submit when budget exhausted.
│       hard_iteration_cap = 50 (safety valve above tool budget).
│
├── prompts/
│   ├── traversal_instructions.md
│   │   Constant 7-step traversal procedure. Baked into container image.
│   │   Covers: orient, survey, drill, read, cite, backtrack, terminate.
│   │   Explains how to use all 4 question spec fields.
│   │   Budget rules and anti-patterns.
│   │
│   ├── field_usage_note.md
│   │   Condensed 4-field usage guide. Baked into container image.
│   │
│   ├── prompt_loader.py
│   │   parse_questionnaire_md(text) — regex extracts 6 <section> tags.
│   │   Reports ALL missing sections at once (not just first).
│   │   load_questionnaire(source, s3) — handles str or S3Ref.
│   │
│   └── prompt_assembler.py
│       assemble_prompt(parsed_md, question, pageindex_summary) → AssembledPrompt
│       _extract_wrapper_instructions() strips everything from --- QUESTION_BLOCK
│       onwards so only instructional text goes into the prompt.
│       Reads constant MD files at module import time (once per cold start).
│
├── validation/
│   ├── preflight.py
│   │   check_pageindex_freshness() — warns if older than staleness_warn_days
│   │   check_documents_accessible() — HEAD each doc S3 URI
│   │   check_question_blocks() — duplicate ID detection
│   │   Returns (errors, warnings) — errors block run, warnings logged.
│   │
│   ├── output_validator.py
│   │   Extracts JSON Schema from <output_schema> section.
│   │   Validates answer against schema using jsonschema library.
│   │   Soft check on label field matching question_label.
│   │
│   └── confidence_check.py
│       compute_confidence() — post-hoc floor from objective signals:
│       - 0 citations → insufficient_evidence
│       - 1 citation → medium
│       - single document → medium
│       - counts_as keywords missing from reasoning → medium
│       Final = min(model_reported, computed_floor).
│       Logs downgraded flag if model overclaimed confidence.
│
└── utils/
    └── parse_question_set.py
        Adapted from provided script. Accepts MD text (not file path).
        extract_question_block_text() — finds --- QUESTION_BLOCK delimiter.
        parse_questions() — parses id/label/metric_def/counts_as/does_not_count/fallback_rule.
        Multi-line values concatenated with spaces.
        parse_question_set_from_text() — top-level convenience function.
```

---

## 10. Infrastructure

### AWS Resources created by Terraform

| Resource | Name pattern | Purpose |
|---|---|---|
| ECR Repository | `{project}-{env}` | Stores ARM64 container images |
| IAM Role | `{project}-{env}-runtime` | Execution role for container |
| IAM Policy: ecr_pull | inline | Pull image from ECR |
| IAM Policy: bedrock_invoke | inline | Call Bedrock Converse API |
| IAM Policy: s3_read | inline | Read pageIndex and PDFs |
| IAM Policy: observability | inline | CloudWatch logs + X-Ray |
| S3 Bucket | `{project}-{env}-inputs-{account}` | pageIndex + questionnaire storage |
| AgentCore Runtime | `{project}_{env}` (underscores) | Container hosting + session management |
| AgentCore Endpoint | `default` | Invocation URL |
| null_resource | `runtime_update` | Forces new runtime version on deploy |

### IAM trust policy

The execution role can only be assumed by `bedrock-agentcore.amazonaws.com` with two conditions:
- `aws:SourceAccount` = your account ID (prevents cross-account abuse)
- `aws:SourceArn` = AgentCore runtime ARN pattern (prevents confused deputy)

### Dockerfile

- Base image: `python:3.12-slim`
- Platform: `linux/arm64` (AWS Graviton, required by AgentCore)
- Non-root user: `agent`
- Port: 8080
- Entrypoint: `python runtime_entrypoint.py`
- SDK: `bedrock-agentcore` handles uvicorn startup on 0.0.0.0:8080

---

## 11. Deployment

### First-time setup

```bash
# 1. Configure
cd infra
cp terraform.tfvars.example terraform.tfvars
# Edit: aws_region, project_name, environment, existing_role_arn (if applicable)

# 2. Bootstrap (creates ECR + IAM + S3, NOT runtime)
terraform init
cd ..
./scripts/bootstrap.sh

# 3. Upload assets to S3
BUCKET=$(cd infra && terraform output -raw input_bucket)
aws s3 cp pageindex.json   s3://$BUCKET/company/pageindex.json
aws s3 cp questionnaire.md s3://$BUCKET/questionnaires/water.md

# 4. Update payload
sed -i "s/YOUR_INPUT_BUCKET/$BUCKET/g" scripts/sample_payload.json

# 5. Deploy (build image + create runtime)
./scripts/deploy.sh v1
```

### Every subsequent deploy

```bash
./scripts/deploy.sh v2   # or $(git rev-parse --short HEAD)
```

### Testing

```bash
python3 scripts/invoke.py \
    --endpoint-arn "$(cd infra && terraform output -raw runtime_arn)" \
    --payload scripts/sample_payload.json \
    --region us-east-1
```

---

## 12. Flask API Integration

### How app.py uses the agent

`app.py` is a separate Flask application that invokes the AgentCore runtime and stores 
results. It never modifies agent code.

```
app.py flow for one company run:
1. POST /api/answering-agent/run {"company": "Paccar"}
2. Auto-resolve pageindex_s3_uri from pageindex-runs DynamoDB
3. List all .md files from S3 questionnaires/ prefix
4. Write initial row to answering-runs (status=running)
5. Background thread starts

For each MD file (parallel, ANSWERING_MD_CONCURRENCY concurrent):
  a. invoke_agent_runtime() → blocks until container completes
  b. Read RunResult from response body
  c. Write each question result to answering-results DynamoDB
  d. Update answering-runs (md_done += 1)

6. All MD files done → status = complete
```

### Runtime invocation payload

```json
{
  "run_id": "abc123",
  "company": "Paccar",
  "pageindex": {
    "s3_uri": "s3://bucket/paccar/paccar_pageindex.json"
  },
  "questionnaire_md": {
    "s3_uri": "s3://bucket/questionnaires/code_of_conduct.md"
  }
}
```

### Runtime response

```json
{
  "status": "ok",
  "result": {
    "run_id": "abc123",
    "company": "Paccar",
    "company_slug": "paccar",
    "md_file": "code_of_conduct.md",
    "category": "Code Of Conduct",
    "results": [
      {
        "question_id": "Q1",
        "question_label": "...",
        "answer_payload": {"value": "addressed"},
        "confidence": {"final": "high", "model_reported": "high", "downgraded": false},
        "citations": [{"quoted_span": "...", "doc_name": "...", "page_start": 4}],
        "tool_calls_used": 8,
        "flags": [],
        "error": null
      }
    ],
    "summary_stats": {"n_questions": 8, "n_errors": 0}
  }
}
```

---

## 13. Frontend Dashboard

Three-level drill-down in the Reports page:

**Level 1 — Company list** (`/api/answering-agent/companies`)
Cards showing each company with last run date, status, categories done/total.
"Run Answering Agent" button triggers a new run and polls for progress.

**Level 2 — Category list** (`/api/answering-agent/companies/<slug>`)
One card per questionnaire MD file. Shows category name (derived from filename) 
and question count.

**Level 3 — Q&A table** (`/api/answering-agent/companies/<slug>/<category>`)
Expandable rows showing:
- Question label + confidence badge
- Answer value
- Citation box: verbatim quote, document name, page numbers, node path, S3 link
- Flags (budget_exhausted, fallback_fired, confidence_downgraded etc.)

Filter by question text and confidence level. Click any row to expand.

---

## 14. DynamoDB Schema

### `answering-runs`

```
PK: run_id (String)

Fields:
  run_id           UUID for this company run
  company          Display name e.g. "Paccar"
  company_slug     Slugified e.g. "paccar"
  pageindex_s3_uri S3 URI of the pageIndex used
  status           running | complete | failed
  started_at       ISO timestamp
  finished_at      ISO timestamp
  heartbeat_at     Updated after each MD file completes
  md_total         Total number of MD files
  md_done          Number of MD files completed so far
  md_files         JSON array of MD filenames
  error_msg        Error message if failed
```

### `answering-results`

```
PK: run_id    (String)
SK: result_id (String)   format: "md_file#question_id"
                         e.g.   "code_of_conduct.md#Q1"

Fields:
  run_id          Same as PK — links back to answering-runs
  result_id       md_file#question_id
  company_slug    For filtering/scanning by company
  company         Display name
  md_file         e.g. "code_of_conduct.md"
  category        e.g. "Code Of Conduct"
  question_id     e.g. "Q1"
  question_label  Full question text
  answer          JSON string of answer_payload
  confidence      Final confidence level string
  confidence_full JSON string of full ConfidenceBreakdown
  citations       JSON array of citation objects
  flags           JSON array of AnswerFlag strings
  tool_calls_used Integer
  error           Error message or empty string
  created_at      ISO timestamp
```

### `pageindex-runs`

```
PK: company  (String)
SK: run_id   (String)

Fields:
  company          Display name (PK)
  run_id           UUID (SK)
  s3_prefix        S3 prefix searched for PDFs
  status           pending | running | complete | no_results | failed
  pageindex_s3_uri S3 URI where pageIndex was saved
  indexed          JSON array of indexed s3_keys
  skipped          JSON array of skipped s3_keys
  started_at       ISO timestamp
  finished_at      ISO timestamp
  error_msg        Error message if failed
```

---

## 15. Configuration Reference

### `terraform.tfvars` — key settings

| Variable | Default | Description |
|---|---|---|
| `project_name` | `pageindex-agent` | Prefix for all resource names |
| `environment` | `dev` | Appended to resource names |
| `aws_region` | `us-east-1` | Must match Bedrock model access region |
| `image_tag` | `latest` | Docker image tag — use git SHA in prod |
| `bedrock_model_id` | `amazon.nova-pro-v1:0` | Change to `anthropic.claude-sonnet-4-5` |
| `tool_call_budget` | `15` | Max tool calls per question |
| `max_page_span` | `15` | Max pages per fetch_pages call |
| `max_parallel_questions` | `1` | Parallel questions within one runtime call |
| `max_output_tokens` | `8096` | Max output tokens for model response |
| `existing_role_arn` | `""` | Set to skip IAM creation |
| `create_input_bucket` | `true` | Set false to use existing bucket |
| `max_session_lifetime_seconds` | `3600` | Max AgentCore session lifetime |
| `idle_session_timeout_seconds` | `900` | Idle session reaping threshold |

### `app.py` env vars — answering agent

| Variable | Default | Description |
|---|---|---|
| `ANSWERING_RUNTIME_ARN` | hardcoded ARN | AgentCore runtime ARN |
| `ANSWERING_RUNS_TABLE` | `answering-runs` | DynamoDB table for run status |
| `ANSWERING_RESULTS_TABLE` | `answering-results` | DynamoDB table for results |
| `QUESTIONNAIRES_PREFIX` | `questionnaires/` | S3 prefix for MD files |
| `QUESTIONNAIRES_BUCKET` | `REPORTS_BUCKET` | S3 bucket for MD files |
| `ANSWERING_MD_CONCURRENCY` | `3` | Parallel MD file processing |

---

## 16. Known Issues and Fixes

### PageIndex Issues

**`end_index < start_index`**
Some nodes have reversed page ranges. Fix the pageIndex JSON before uploading — 
run the diagnostic Python script to find all broken nodes.

**All documents named "Untitled"**
The pageIndex builder used PDF metadata title instead of S3 filename. The agent 
cannot disambiguate documents and cannot fetch the correct PDF. Rebuild the pageIndex 
with the S3 key filename as `doc_name`.

### Model Issues

**`ModelErrorException` — invalid tool use sequence**
Transient error from Nova Pro. Fixed by switching to Claude Sonnet 4.5 and adding 
3-attempt retry with exponential backoff in `bedrock_client.py`.

**`temperature` deprecated for Sonnet**
Remove `temperature` from `inferenceConfig`. Sonnet does not accept it.

**`loop.model_refused_submit` — model ignores tools**
Sonnet requires `toolChoice: {"auto": {}}` in the Converse request. Without it 
the model responds in plain text and never calls any tools.

**`loop.unexpected_stop: max_tokens`**
Model hits output token limit mid-response. Fixed by increasing `max_output_tokens` 
to 8096 and adding a nudge handler that tells the model to submit immediately.

### Timeout Issues

**AgentCore read timeout**
The container completes but the response is not delivered within the boto3 
`read_timeout`. Fix: increase to 1200s in `get_agentcore()`. Reduce per-invocation 
runtime by lowering `tool_call_budget` or increasing `max_parallel_questions`.

**AgentCore synchronous invoke limit**
Invocations taking 800+ seconds may not deliver the response even after container 
completion. Keep each runtime call under 600 seconds by limiting questions per MD 
file or increasing parallelism.

### DynamoDB Issues

**Schema mismatch on `answering-runs`**
Table must have `PK=run_id` only (no SK). Recreate if it was created with 
`PK=session_id, SK=run_id`.

**Empty `md_file` causing result overwrites**
If `md_file` is empty the `result_id` becomes `#Q1` for all MD files and rows 
overwrite each other. Fix in `pipeline.py`: handle both `S3Ref` and `dict` when 
deriving `md_file` from `payload.questionnaire_md`.

**`n_questions: 0`**
The `--- QUESTION_BLOCK` delimiter was not found in the MD file. Check the exact 
format of the delimiter in your MD file — must be 3+ dashes followed by 
`QUESTION_BLOCK` (case-insensitive).
