# Deployment Guide

Deploy the two stacks separately, in this order:

1. Deploy `reportiq-ecs` to create or update the portal, ECS cluster, task
   security group, and subnets.
2. Deploy `infra/agentcore-report` to create/update the AgentCore runtime and
   browser-worker ECS service on that existing Report IQ cluster.
3. Redeploy `reportiq-ecs` with the browser-result SQS queue values so the UI
   can update long-running browser jobs.

## Prerequisites

- AWS CLI authenticated to the target account and region.
- Docker Desktop running, with `buildx` available.
- Terraform version required by each stack.
- A real SEC contact value in `SEC_USER_AGENT`, for example
  `Report IQ operations@example.com`.
- Private subnets used by the browser worker must have NAT egress. AWS VPC
  endpoints alone cannot access public company websites.

## 1. Deploy Report IQ ECS

Configure the portal stack:

```bash
cd /Users/abhay/Documents/csa-project/reportiq-ecs
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
```

Set the correct `vpc_id`, `subnet_ids`, runtime ARN, bucket, and DynamoDB table
names in `terraform/terraform.tfvars`. Then deploy:

```bash
./scripts/deploy.sh v1
```

Record the shared ECS values for the browser-worker deployment:

```bash
cd terraform
terraform output -raw ecs_cluster
terraform output -json ecs_subnet_ids
terraform output -raw ecs_task_security_group_id
```

## 2. Deploy AgentCore And Browser Worker

Configure the AgentCore stack:

```bash
cd /Users/abhay/Documents/csa-project/infra/agentcore-report
cp terraform.tfvars.example terraform.tfvars
```

Set the normal AgentCore settings and copy the Report IQ values from step 1:

```hcl
enable_fargate_browser_worker = true
fargate_ecs_cluster_id        = "reportiq-cluster"
fargate_subnet_ids            = ["subnet-...", "subnet-..."]
fargate_security_group_ids    = ["sg-..."]
fargate_assign_public_ip      = false

sec_user_agent = "Report IQ operations@example.com"
use_browser    = true
```

Initialize Terraform and create both ECR repositories before building images:

```bash
terraform init
terraform apply -auto-approve \
  -target=aws_ecr_repository.agent \
  -target=aws_ecr_repository.browser_worker
./scripts/deploy.sh v1
```

The browser worker is an ECS service in the existing Report IQ cluster. It does
not create a separate ECS cluster, subnet, or task security group.

Capture the asynchronous-result queue details:

```bash
terraform output -raw browser_worker_results_queue_url
terraform output -raw browser_worker_results_queue_arn
```

## 3. Connect Browser Results To Report IQ

Add the two output values from step 2 to
`reportiq-ecs/terraform/terraform.tfvars`:

```hcl
browser_results_queue_url = "https://sqs.us-east-1.amazonaws.com/ACCOUNT/QUEUE"
browser_results_queue_arn = "arn:aws:sqs:us-east-1:ACCOUNT:QUEUE"
```

Redeploy the portal to give it the queue URL and least-privilege SQS receive
permission:

```bash
cd /Users/abhay/Documents/csa-project/reportiq-ecs
./scripts/deploy.sh v2
```

## Verify

Check the portal service and logs:

```bash
cd /Users/abhay/Documents/csa-project/reportiq-ecs/terraform
aws ecs describe-services \
  --cluster "$(terraform output -raw ecs_cluster)" \
  --services "$(terraform output -raw ecs_service)" \
  --region us-east-1
aws logs tail "$(terraform output -raw log_group)" --follow --region us-east-1
```

Submit a company name and its official website in Report IQ. The portal sends a
single typed request to AgentCore. The agent tries official website discovery,
site-scoped search, AgentCore Browser, and finally the Fargate worker. A report
is stored only after source, company, document class, and year validation.

For the tier diagram, see
[`infra/agentcore-report/docs/retrieval-flow.md`](infra/agentcore-report/docs/retrieval-flow.md).
