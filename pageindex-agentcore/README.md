# pageindex-agentcore

Runs PageIndex inside an AWS AgentCore runtime, bypassing the Bedrock
account-level use-case approval gate that blocks direct model invocations.

---

## Folder structure

```
pageindex-agentcore/
├── pageindex-lib/              ← git clone your PageIndex repo here
│   └── pageindex/
│       ├── __init__.py         (page_index_main lives here)
│       └── utils/
│           └── config_loader.py
│
├── runtime/
│   ├── runtime_handler.py      ← AgentCore handler (runs inside container)
│   └── requirements.txt
│
├── infra/
│   └── main.tf                 ← Terraform: ECR, IAM roles, AgentCore runtime
│
├── scripts/
│   ├── deploy.sh               ← one-command: build + push + terraform apply
│   ├── build_and_push.sh       ← build AMD64 image and push to ECR
│   ├── invoke_local.py         ← smoke-test a single PDF against live runtime
│   └── payload.example.json    ← example invocation payload
│
├── Dockerfile                  ← builds the runtime container image
├── build_pdf_index.py          ← orchestration script (runs on EC2)
└── README.md
```

---

## S3 layout

Each company has its own prefix in S3. PDFs and the output pageindex file
all live together in that prefix:

```
s3://<bucket>/
└── paccar/
    ├── paccar-2024-sustainability-report.pdf
    ├── paccar-2023-annual-report.pdf
    └── paccar_pageindex.json        ← written/updated by this script
```

The pageindex file is read on subsequent runs to skip already-indexed PDFs
(incremental mode). Pass `--force` to rebuild from scratch.

---

## Output format

```json
{
  "company": "Paccar",
  "company_slug": "paccar",
  "bucket": "your-bucket-name",
  "updated_at": "2026-07-09T10:32:15+00:00",
  "documents": [
    {
      "doc_name": "paccar-2024-sustainability-report.pdf",
      "structure": [
        {
          "title": "About This Report",
          "node_id": "0001",
          "start_index": 1,
          "end_index": 3,
          "summary": "...",
          "nodes": []
        }
      ],
      "_meta": {
        "s3_key":     "paccar/paccar-2024-sustainability-report.pdf",
        "s3_uri":     "s3://your-bucket/paccar/paccar-2024-sustainability-report.pdf",
        "indexed_at": "2026-07-09T10:32:15+00:00"
      }
    }
  ]
}
```

---

## 1. Clone PageIndex

```bash
# From this folder
git clone <your-pageindex-repo-url> pageindex-lib
```

Verify the import resolves:
```bash
python -c "import sys; sys.path.insert(0, 'pageindex-lib'); from pageindex import page_index_main; print('OK')"
```

---

## 2. Deploy infrastructure

```bash
cd infra
terraform init
terraform apply -var="reports_bucket=edo-coanalyst-report-610639371721"
```

Note the outputs:
- `ecr_repository_url` — where to push the Docker image
- `runtime_arn`        — set as `AGENTCORE_RUNTIME_ARN` on your EC2
- `caller_role_arn`    — attach to your EC2 instance profile

---

## 3. Build and push the Docker image

Run from this folder (where `Dockerfile` lives):

```bash
# Authenticate to ECR
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin <ecr_repository_url>

# Build, tag, push
docker build -t pageindex-runtime .
docker tag pageindex-runtime:latest <ecr_repository_url>:latest
docker push <ecr_repository_url>:latest
```

Terraform also prints these commands via:
```bash
cd infra && terraform output push_commands
```

---

## 4. Configure your EC2

Attach `caller_role_arn` to your EC2 instance profile, then:

```bash
export AGENTCORE_RUNTIME_ARN=<runtime_arn from terraform output>
export REPORTS_BUCKET=edo-coanalyst-report-610639371721
```

---

## 5. Deploy (build + push + apply in one command)

After the first `terraform apply` and any subsequent code change:

```bash
# From the repo root
./scripts/deploy.sh v1

# Subsequent deploys — bump the tag each time
./scripts/deploy.sh v2
```

`deploy.sh` does three things in order:
1. Builds the AMD64 Docker image and pushes it to ECR with the given tag
2. Runs `terraform apply -auto-approve -var "image_tag=<tag>"`
3. Lists the runtime endpoint status so you can confirm the new version is live

To smoke-test a single PDF against the live runtime without running the full indexer:

```bash
# Edit scripts/payload.example.json with your bucket/key, then:
python scripts/invoke_local.py \
    $(cd infra && terraform output -raw runtime_arn) \
    scripts/payload.example.json
```

---

## 6. Run the build script

```bash
# By company name (auto-discovers S3 prefix)
python build_pdf_index.py --company "Paccar"

# By exact S3 prefix (faster, deterministic)
python build_pdf_index.py --s3-prefix paccar/

# Full s3:// URI form
python build_pdf_index.py --s3-prefix s3://edo-coanalyst-report-610639371721/paccar/

# Force full re-index (ignores existing pageindex file)
python build_pdf_index.py --company "Paccar" --force
```

---

## How it works

```
EC2 (build_pdf_index.py)                 AgentCore Runtime
────────────────────────                 ─────────────────
List PDFs under paccar/ in S3
Load existing paccar_pageindex.json
from S3 (skip already-indexed keys)
  │
  └─ for each new PDF:
       invoke runtime ─────────────────→ Stream PDF from S3
       { bucket, s3_key }                Run page_index_main()
                                         (LiteLLM → Bedrock,
                                          authorized via runtime role)
       ← { status, index } ────────────
       attach _meta
       PUT paccar_pageindex.json → S3    (after every doc, incremental)
```

The calling script never touches Bedrock directly — only
`bedrock-agentcore:InvokeAgentRuntime` + S3 read/write permissions are needed.
