# Infrastructure

Terraform config for the PageIndex ReAct agent on AWS Bedrock AgentCore Runtime.

## What gets created

| Resource | Purpose |
|---|---|
| `aws_ecr_repository.agent` | Holds the ARM64 container image |
| `aws_iam_role.runtime` | Execution role assumed by AgentCore — grants ECR pull, Bedrock InvokeModel, S3 read, CloudWatch/X-Ray |
| `aws_s3_bucket.input` (optional) | Stores pageindex JSON + questionnaire MD files |
| `aws_bedrockagentcore_agent_runtime.this` | The runtime itself — references the ECR image URI + env vars |
| `aws_bedrockagentcore_agent_runtime_endpoint.default` | Callable endpoint used by `InvokeAgentRuntime` |

## First-time deploy

Ordering matters — AgentCore validates the ECR image exists at create time.

```bash
# 0. Prereqs
#    - Terraform >= 1.5.0
#    - AWS credentials with permission to create the resources above
#    - Docker with buildx enabled (or CodeBuild — see notes below)
#    - Enabled model access for amazon.nova-pro-v1:0 in the Bedrock console
#      (Bedrock -> Model access -> request/enable).

# 1. Provision ECR + IAM (skip runtime creation via a targeted apply).
cd infra
cp terraform.tfvars.example terraform.tfvars   # edit as needed
terraform init
terraform apply \
  -target=aws_ecr_repository.agent \
  -target=aws_iam_role.runtime \
  -target=aws_s3_bucket.input

# 2. Build & push the image. AgentCore is ARM64-only.
ECR_URL=$(terraform output -raw ecr_repository_url)
aws ecr get-login-password --region "$(terraform output -raw runtime_endpoint_arn | cut -d: -f4 || echo us-east-1)" \
  | docker login --username AWS --password-stdin "$ECR_URL"

docker buildx build \
  --platform linux/arm64 \
  --tag "$ECR_URL:latest" \
  --push \
  ../agent

# 3. Create the runtime (image now exists in ECR).
terraform apply
```

Subsequent deploys are just:
```bash
docker buildx build --platform linux/arm64 --tag "$ECR_URL:$NEW_TAG" --push ../agent
terraform apply -var="image_tag=$NEW_TAG"
```

## Invoking the runtime

```python
import boto3, json

client = boto3.client("bedrock-agentcore", region_name="us-east-1")
resp = client.invoke_agent_runtime(
    agentRuntimeArn="<runtime_endpoint_arn from tf output>",
    payload=json.dumps({
        "run_id": "test-run-1",
        "pageindex": {"s3_uri": "s3://<bucket>/testcorp/pageindex.json"},
        "questionnaire_md": {"s3_uri": "s3://<bucket>/questionnaires/water.md"},
        "question_set": [
            {
                "id": "Q1",
                "label": "Total water withdrawal in FY2024 (megaliters)",
                "metric_def": "...",
                "counts_as": "...",
                "does_not_count": "...",
                "fallback_rule": "..."
            }
        ]
    }).encode("utf-8"),
)
print(resp["response"].read().decode())
```

## Troubleshooting

- **`RuntimeClientError` (403)** on invoke → check CloudWatch logs at
  `/aws/bedrock-agentcore/runtimes/<runtime_id>-<endpoint_name>/runtime-logs`.
  Almost always a container startup crash: missing dep, wrong platform, or
  the ECR image doesn't exist at the tag you referenced.

- **`exec format error`** in logs → image was built for x86, not ARM64. Rebuild
  with `--platform linux/arm64`.

- **Silent import errors** at startup → same root cause. `docker inspect` the
  image and confirm `Architecture: arm64`.

- **`ValidationException: model not accessible`** → enable model access for
  `amazon.nova-pro-v1:0` in the Bedrock console in the same region.

## Notes

- `image_tag` defaults to `latest`. That's fine for dev but in prod pin to
  a git SHA — that gives you rollback via `terraform apply -var=image_tag=<old>`.
- AgentCore caches by full image URI. If you use `latest`, bumping the
  `_DEPLOY_TIMESTAMP` env var (via `terraform apply -replace=…`) forces a
  refetch. The `lifecycle.ignore_changes` block prevents perpetual drift on
  routine plans.
- VPC mode: set `network_mode = "VPC"` in tfvars and provide `vpc_subnet_ids`
  + `vpc_security_group_ids`. The SGs need egress to Bedrock + S3 endpoints
  (either public or via PrivateLink).
