data "aws_caller_identity" "current" {}

locals {
  name   = var.app_name
  region = var.region
  acct   = var.account_id

  browser_worker_subnet_ids = (
    length(var.browser_worker_subnet_ids) > 0
    ? var.browser_worker_subnet_ids
    : var.subnet_ids
  )
  browser_worker_security_group_ids = (
    length(var.browser_worker_security_group_ids) > 0
    ? var.browser_worker_security_group_ids
    : [aws_security_group.browser_worker.id]
  )

  container_env = {
    AWS_REGION                     = var.region
    QUERIES_TABLE                  = var.queries_table
    PROVENANCE_TABLE               = var.provenance_table
    RUNS_TABLE                     = var.runs_table
    REPORTS_BUCKET                 = var.reports_bucket
    AGENT_RUNTIME_ARN              = var.agent_runtime_arn
    AGENT_QUALIFIER                = var.agent_qualifier
    BROWSER_WORKER_ENABLED         = tostring(var.enable_browser_worker)
    BROWSER_JOBS_TABLE             = aws_dynamodb_table.browser_jobs.name
    BROWSER_ECS_CLUSTER            = aws_ecs_cluster.main.arn
    BROWSER_ECS_TASK_DEFINITION    = aws_ecs_task_definition.browser_worker.arn
    BROWSER_ECS_CONTAINER_NAME     = "browser-worker"
    BROWSER_ECS_SUBNET_IDS         = join(",", local.browser_worker_subnet_ids)
    BROWSER_ECS_SECURITY_GROUP_IDS = join(",", local.browser_worker_security_group_ids)
    BROWSER_ECS_ASSIGN_PUBLIC_IP   = tostring(var.browser_worker_assign_public_ip)
    BULK_COMPANY_CONCURRENCY       = tostring(var.bulk_company_concurrency)
    STATIC_DIR                     = "/app/static"
    PORT                           = "8080"
  }

  browser_worker_env = {
    AWS_REGION                            = var.region
    QUERIES_TABLE                         = var.queries_table
    PROVENANCE_TABLE                      = var.provenance_table
    RUNS_TABLE                            = var.runs_table
    REPORTS_BUCKET                        = var.reports_bucket
    BROWSER_JOBS_TABLE                    = aws_dynamodb_table.browser_jobs.name
    CHROMIUM_PATH                         = "/usr/bin/chromium"
    BROWSER_WORKER_MAX_ATTEMPTS           = tostring(var.browser_worker_max_attempts)
    BROWSER_WORKER_RETRY_DELAY_SECONDS    = tostring(var.browser_worker_retry_delay_seconds)
    BROWSER_WORKER_NAV_TIMEOUT_MS         = tostring(var.browser_worker_nav_timeout_ms)
    BROWSER_WORKER_MAX_DOCUMENT_BYTES     = tostring(var.browser_worker_max_document_bytes)
    BROWSER_WORKER_RUN_PATCH_WAIT_SECONDS = tostring(var.browser_worker_run_patch_wait_seconds)
  }
}

# ════════════════════════════════════════════════════════════════════════════
#  ECR repository
# ════════════════════════════════════════════════════════════════════════════
resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# ════════════════════════════════════════════════════════════════════════════
#  DynamoDB tables (optional — only if manage_dynamo_tables = true)
# ════════════════════════════════════════════════════════════════════════════
resource "aws_dynamodb_table" "queries" {
  count        = var.manage_dynamo_tables ? 1 : 0
  name         = var.queries_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "query_id"

  attribute {
    name = "query_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "runs" {
  count        = var.manage_dynamo_tables ? 1 : 0
  name         = var.runs_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"

  attribute {
    name = "run_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "browser_jobs" {
  name         = var.browser_jobs_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = { Name = var.browser_jobs_table }
}

# ════════════════════════════════════════════════════════════════════════════
#  CloudWatch log group
# ════════════════════════════════════════════════════════════════════════════
resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "browser_worker" {
  name              = "/ecs/${local.name}-browser-worker"
  retention_in_days = 30
}

# ════════════════════════════════════════════════════════════════════════════
#  IAM — task execution role (pull image, write logs)
# ════════════════════════════════════════════════════════════════════════════
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_proxy_secret" {
  count = var.browser_worker_proxy_secret_arn != "" ? 1 : 0

  statement {
    sid       = "ReadBrowserProxySecret"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.browser_worker_proxy_secret_arn]
  }
}

resource "aws_iam_role_policy" "execution_proxy_secret" {
  count  = var.browser_worker_proxy_secret_arn != "" ? 1 : 0
  name   = "${local.name}-browser-proxy-secret"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_proxy_secret[0].json
}

# ════════════════════════════════════════════════════════════════════════════
#  IAM — task role (the app's own permissions: DynamoDB, S3, AgentCore)
# ════════════════════════════════════════════════════════════════════════════
resource "aws_iam_role" "task" {
  name               = "${local.name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "task_perms" {
  statement {
    sid    = "DynamoPortalTables"
    effect = "Allow"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:DeleteItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:DescribeTable",
    ]
    resources = [
      "arn:aws:dynamodb:${local.region}:${local.acct}:table/${var.queries_table}",
      "arn:aws:dynamodb:${local.region}:${local.acct}:table/${var.runs_table}",
      "arn:aws:dynamodb:${local.region}:${local.acct}:table/${var.provenance_table}",
      aws_dynamodb_table.browser_jobs.arn,
      aws_dynamodb_table.pageindex_runs.arn,
      aws_dynamodb_table.answering_runs.arn,
      aws_dynamodb_table.answering_results.arn,
      "arn:aws:dynamodb:${local.region}:${local.acct}:table/${var.queries_table}/index/*",
      "arn:aws:dynamodb:${local.region}:${local.acct}:table/${var.runs_table}/index/*",
      "arn:aws:dynamodb:${local.region}:${local.acct}:table/${var.provenance_table}/index/*",
      "${aws_dynamodb_table.pageindex_runs.arn}/index/*",
      "${aws_dynamodb_table.answering_runs.arn}/index/*",
      "${aws_dynamodb_table.answering_results.arn}/index/*",
    ]
  }

  statement {
    sid    = "S3ReportsBucket"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:ListBucket",
      "s3:ListBucketVersions",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]
    resources = [
      "arn:aws:s3:::${var.reports_bucket}",
      "arn:aws:s3:::${var.reports_bucket}/*",
    ]
  }

  statement {
    sid       = "AgentCoreInvoke"
    effect    = "Allow"
    actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = ["arn:aws:bedrock-agentcore:${local.region}:${local.acct}:runtime/*"]
  }

  statement {
    sid     = "LaunchBrowserWorker"
    effect  = "Allow"
    actions = ["ecs:RunTask"]
    resources = [
      "arn:aws:ecs:${local.region}:${local.acct}:task-definition/${local.name}-browser-worker:*"
    ]
  }

  statement {
    sid       = "TagBrowserWorker"
    effect    = "Allow"
    actions   = ["ecs:TagResource"]
    resources = ["arn:aws:ecs:${local.region}:${local.acct}:task/*"]
    condition {
      test     = "StringEquals"
      variable = "ecs:CreateAction"
      values   = ["RunTask"]
    }
  }

  statement {
    sid     = "PassBrowserWorkerRoles"
    effect  = "Allow"
    actions = ["iam:PassRole"]
    resources = [
      aws_iam_role.execution.arn,
      aws_iam_role.task.arn,
    ]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${local.name}-task-permissions"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_perms.json
}

# ════════════════════════════════════════════════════════════════════════════
#  Security groups
# ════════════════════════════════════════════════════════════════════════════
resource "aws_security_group" "alb" {
  name        = "${local.name}-alb-sg"
  description = "Report IQ ALB - internal HTTP/HTTPS"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTP from internal network (redirects to 443)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = var.alb_ingress_cidrs
  }

  ingress {
    description = "HTTPS from internal network"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.alb_ingress_cidrs
  }

  tags = { Name = "${local.name}-alb-sg" }
}

resource "aws_security_group_rule" "alb_egress_to_tasks" {
  type                     = "egress"
  description              = "Forward traffic to ECS tasks"
  from_port                = 8080
  to_port                  = 8080
  protocol                 = "tcp"
  security_group_id        = aws_security_group.alb.id
  source_security_group_id = aws_security_group.tasks.id
}

resource "aws_security_group" "tasks" {
  name        = "${local.name}-tasks-sg"
  description = "Report IQ ECS tasks"
  vpc_id      = var.vpc_id

  ingress {
    description     = "App port from ALB only"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    description = "HTTPS to VPC endpoints (ECR, CloudWatch Logs)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-tasks-sg" }
}

resource "aws_security_group" "browser_worker" {
  name        = "${local.name}-browser-worker-sg"
  description = "Report IQ one-off browser worker; no inbound traffic"
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS to official report sources, AWS APIs, or approved proxy"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-browser-worker-sg" }
}

# ════════════════════════════════════════════════════════════════════════════
#  Application Load Balancer (internal)
# ════════════════════════════════════════════════════════════════════════════
resource "aws_lb" "app" {
  name               = "${local.name}-internal-alb"
  internal           = true
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.subnet_ids

  tags = { Name = "${local.name}-internal-alb" }
}

resource "aws_lb_target_group" "app" {
  name        = "${local.name}-tg"
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip" # Fargate uses awsvpc → IP targets

  health_check {
    path                = "/health"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  deregistration_delay = 30
}

# Listeners (HTTP redirect + HTTPS) are defined in https.tf

# ════════════════════════════════════════════════════════════════════════════
#  ECS cluster, task definition, service
# ════════════════════════════════════════════════════════════════════════════
resource "aws_ecs_cluster" "main" {
  name = "${local.name}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_task_definition" "browser_worker" {
  family                   = "${local.name}-browser-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.browser_worker_cpu
  memory                   = var.browser_worker_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([
    {
      name      = "browser-worker"
      image     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
      essential = true
      command   = ["python", "/app/backend/browser_worker.py"]

      environment = [
        for k, v in local.browser_worker_env :
        { name = k, value = tostring(v) }
      ]

      secrets = (
        var.browser_worker_proxy_secret_arn != ""
        ? [{
          name      = "BROWSER_OUTBOUND_PROXY"
          valueFrom = var.browser_worker_proxy_secret_arn
        }]
        : []
      )

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.browser_worker.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "ecs"
        }
      }
    }
  ])

  tags = { Name = "${local.name}-browser-worker" }

  depends_on = [
    aws_iam_role_policy.task,
    aws_iam_role_policy.execution_proxy_secret,
  ]
}

resource "aws_ecs_task_definition" "app" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([
    {
      name      = local.name
      image     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
      essential = true

      portMappings = [
        {
          containerPort = 8080
          protocol      = "tcp"
        }
      ]

      environment = [for k, v in local.container_env : { name = k, value = tostring(v) }]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "ecs"
        }
      }

      # Container-level health check uses Python (always present in the image).
      # The ALB target-group health check (/health) is the primary gate.
      healthCheck = {
        command     = ["CMD-SHELL", "python3 -c 'import urllib.request; urllib.request.urlopen(\"http://localhost:8080/health\")' || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 40
      }
    }
  ])
}

resource "aws_ecs_service" "app" {
  name            = local.name
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = var.assign_public_ip
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = local.name
    container_port   = 8080
  }

  health_check_grace_period_seconds = 60

  depends_on = [aws_lb_listener.https, aws_lb_listener.http_redirect]

  lifecycle {
    ignore_changes = [desired_count]
  }
}
