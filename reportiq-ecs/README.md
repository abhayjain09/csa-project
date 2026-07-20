# Report IQ — ECS Fargate Deployment

Containerised version of the Report IQ portal running on **ECS Fargate** behind an
internal ALB. No EC2 to manage.

```
reportiq-ecs/
├── app/                       ← the container
│   ├── Dockerfile
│   ├── .dockerignore
│   ├── backend/app.py
│   ├── backend/requirements.txt
│   └── static/index.html
├── terraform/                 ← all infra
│   ├── providers.tf
│   ├── variables.tf
│   ├── main.tf                ← ECR, ECS, ALB, IAM, SGs, DynamoDB
│   ├── outputs.tf
│   └── terraform.tfvars.example
└── scripts/
    ├── discover_network.sh    ← finds your VPC + subnets
    ├── build_and_push.sh      ← docker build + push to ECR
    └── deploy.sh              ← full one-shot deploy
```

---

## What gets created

| Resource | Name |
|----------|------|
| ECR repo | `reportiq` |
| ECS cluster | `reportiq-cluster` (Fargate, Container Insights on) |
| ECS service + task | `reportiq` (1 task, 0.5 vCPU / 1 GB) |
| Browser fallback task | `reportiq-browser-worker` (one-off; same cluster and image) |
| Internal ALB | `reportiq-internal-alb` |
| Target group | `reportiq-tg` (IP target, /health check) |
| Security groups | `reportiq-alb-sg`, `reportiq-tasks-sg` |
| IAM roles | `reportiq-ecs-execution`, `reportiq-ecs-task` |
| CloudWatch log groups | `/ecs/reportiq`, `/ecs/reportiq-browser-worker` |
| DynamoDB tables | `reportiq-web-queries`, `reportiq-runs`, `reportiq-browser-jobs` |

Existing S3 bucket, provenance table, and AgentCore runtime are **referenced, not modified**.

---

## Deploy (3 steps from your Mac)

### 1. Discover your network
```bash
chmod +x scripts/*.sh
./scripts/discover_network.sh
```
Copy the suggested `vpc_id` and `subnet_ids` lines.

### 2. Configure
```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
# paste vpc_id + subnet_ids from step 1
# Since you already created the tables via CLI, keep:
#   manage_dynamo_tables = false
```

### 3. Deploy everything
```bash
./scripts/deploy.sh
```

This does: terraform init → create ECR → build+push image → full apply.
At the end it prints the portal URL (`http://reportiq-internal-alb-xxxx.us-east-1.elb.amazonaws.com`).

---

## Redeploy after a code change

```bash
./scripts/deploy.sh v2          # new tag → new task definition → rolling deploy
```

Or to redeploy the same tag (force pull):
```bash
cd terraform
aws ecs update-service \
  --cluster $(terraform output -raw ecs_cluster) \
  --service $(terraform output -raw ecs_service) \
  --force-new-deployment --region us-east-1
```

---

## Architecture choice

- **ARM64 / Graviton** by default (cheaper, and builds natively on Apple Silicon).
  To use x86: set `cpu_architecture = "X86_64"` in tfvars **and** `ARCH="amd64"` in `build_and_push.sh`.
- **Stateless**: run status is read from DynamoDB, so multiple tasks / restarts are safe.
  Scale with `desired_count`.
- The background AgentCore invocation runs in a task thread; results land in DynamoDB
  regardless of which task serves the later status poll.

---

## Networking note

The ALB is **internal** — its DNS resolves to a private `10.164.56.x` IP, same as the EC2.
If your workstation can't route to that subnet, ECS does not change that; you'd still need:
- a network-team route from your workstation subnet to the VPC, or
- VDI / jump-host browser access on the internal network, or
- (last resort) flip the ALB to `internal = false` in a **public** subnet with tight
`alb_ingress_cidrs` — only if your security policy allows it.

### WAF browser fallback

The portal only launches the one-off browser task when AgentCore returns the
typed `blocked_by_source_waf` status with exact official PDF candidates. The
worker keeps a Chromium session alive across bounded retries, then verifies the
company and report class before writing to S3.

It is disabled by default because the worker requires an approved outbound
route. Enable it after configuring public HTTPS egress through NAT, a Transit
Gateway, public worker subnets, or a reachable approved proxy.

For private subnets whose Transit Gateway provides controlled public egress:

```hcl
enable_browser_worker           = true
browser_worker_assign_public_ip = false
```

For public worker subnets:

```hcl
enable_browser_worker           = true
browser_worker_subnet_ids       = ["subnet-public-a", "subnet-public-b"]
browser_worker_assign_public_ip = true
```

or an approved reachable proxy stored in Secrets Manager:

```hcl
enable_browser_worker             = true
browser_worker_proxy_secret_arn   = "arn:aws:secretsmanager:us-east-1:ACCOUNT:secret:reportiq-browser-proxy"
```

The secret value may be a URL or JSON:

```json
{"server":"https://proxy.example:443","username":"user","password":"secret"}
```

This fallback does not solve CAPTCHAs or bypass access controls. If the official
source blocks both the normal and approved proxy egress, the job remains a
typed source-blocked failure and the existing manual upload remains available.

---

## Troubleshooting

**Task won't start / unhealthy:**
```bash
aws logs tail /ecs/reportiq --follow --region us-east-1
aws ecs describe-services --cluster reportiq-cluster --services reportiq \
  --region us-east-1 --query 'services[0].events[0:5]'
```

**Image pull errors:** make sure the task is in a subnet with a NAT gateway or
VPC endpoints for ECR (`com.amazonaws.us-east-1.ecr.dkr`, `.ecr.api`, `.s3`).
If subnets are private with no NAT, add those VPC endpoints.

**Target unhealthy but task running:** check the security group — `reportiq-tasks-sg`
must allow 8080 from `reportiq-alb-sg` (Terraform sets this automatically).

**Browser fallback status:**
```bash
aws logs tail /ecs/reportiq-browser-worker --follow --region us-east-1
aws dynamodb get-item --table-name reportiq-browser-jobs \
  --key '{"job_id":{"S":"JOB_ID"}}' --region us-east-1
```
