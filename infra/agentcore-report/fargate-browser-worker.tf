# Optional long-running browser worker. It is intentionally disabled by default:
# the caller must provide approved subnets and security groups before Terraform
# creates an internet-facing browser workload.

resource "aws_sqs_queue" "browser_dead_letter" {
  count                     = var.enable_fargate_browser_worker ? 1 : 0
  name                      = "${var.name}-browser-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "browser_jobs" {
  count                      = var.enable_fargate_browser_worker ? 1 : 0
  name                       = "${var.name}-browser-jobs"
  visibility_timeout_seconds = 900
  receive_wait_time_seconds  = 20
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.browser_dead_letter[0].arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue" "browser_results" {
  count                     = var.enable_fargate_browser_worker ? 1 : 0
  name                      = "${var.name}-browser-results"
  message_retention_seconds = 1209600
}

resource "aws_cloudwatch_log_group" "browser_worker" {
  count             = var.enable_fargate_browser_worker ? 1 : 0
  name              = "/aws/ecs/${var.name}-browser-worker"
  retention_in_days = 90
}

data "aws_iam_policy_document" "ecs_task_trust" {
  count = var.enable_fargate_browser_worker ? 1 : 0
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "browser_worker_execution" {
  count              = var.enable_fargate_browser_worker ? 1 : 0
  name               = "${var.name}-browser-worker-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_trust[0].json
}

resource "aws_iam_role_policy_attachment" "browser_worker_execution" {
  count      = var.enable_fargate_browser_worker ? 1 : 0
  role       = aws_iam_role.browser_worker_execution[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "browser_worker_task" {
  count              = var.enable_fargate_browser_worker ? 1 : 0
  name               = "${var.name}-browser-worker-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_trust[0].json
}

data "aws_iam_policy_document" "browser_worker_task" {
  count = var.enable_fargate_browser_worker ? 1 : 0
  statement {
    sid       = "ReadBrowserJobs"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
    resources = [aws_sqs_queue.browser_jobs[0].arn]
  }
  statement {
    sid       = "WriteBrowserResults"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.browser_results[0].arn]
  }
  statement {
    sid       = "WriteValidatedReports"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.reports.arn, "${aws_s3_bucket.reports.arn}/*"]
  }
  statement {
    sid       = "WriteProvenance"
    actions   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.provenance.arn, "${aws_dynamodb_table.provenance.arn}/index/*"]
  }
  statement {
    sid = "ConfirmCandidate"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "browser_worker_task" {
  count  = var.enable_fargate_browser_worker ? 1 : 0
  name   = "browser-worker"
  role   = aws_iam_role.browser_worker_task[0].id
  policy = data.aws_iam_policy_document.browser_worker_task[0].json
}

resource "aws_ecs_cluster" "browser_worker" {
  count = var.enable_fargate_browser_worker ? 1 : 0
  name  = "${var.name}-browser-worker"
}

resource "aws_ecs_task_definition" "browser_worker" {
  count                    = var.enable_fargate_browser_worker ? 1 : 0
  family                   = "${var.name}-browser-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "1024"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.browser_worker_execution[0].arn
  task_role_arn            = aws_iam_role.browser_worker_task[0].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "browser-worker"
    image     = local.worker_image_uri
    essential = true
    environment = [
      { name = "APP_REGION", value = local.region },
      { name = "REPORTS_BUCKET", value = aws_s3_bucket.reports.id },
      { name = "PROVENANCE_TABLE", value = aws_dynamodb_table.provenance.name },
      { name = "LLM_MODEL_ID", value = var.llm_model_id },
      { name = "REQUIRE_LLM_VALIDATION", value = tostring(var.require_llm_validation) },
      { name = "FARGATE_BROWSER_QUEUE_URL", value = aws_sqs_queue.browser_jobs[0].id },
      { name = "FARGATE_BROWSER_RESULT_QUEUE_URL", value = aws_sqs_queue.browser_results[0].id },
      { name = "FARGATE_BROWSER_MAX_PAGES", value = "40" },
      { name = "FARGATE_BROWSER_MAX_SECONDS", value = "600" },
      { name = "FARGATE_BROWSER_CLICK_TIMEOUT_MS", value = "20000" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.browser_worker[0].name
        awslogs-region        = local.region
        awslogs-stream-prefix = "browser"
      }
    }
  }])
}

resource "aws_ecs_service" "browser_worker" {
  count           = var.enable_fargate_browser_worker ? 1 : 0
  name            = "${var.name}-browser-worker"
  cluster         = aws_ecs_cluster.browser_worker[0].id
  task_definition = aws_ecs_task_definition.browser_worker[0].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.fargate_subnet_ids
    security_groups  = var.fargate_security_group_ids
    assign_public_ip = var.fargate_assign_public_ip
  }

  lifecycle {
    precondition {
      condition     = length(var.fargate_subnet_ids) > 0 && length(var.fargate_security_group_ids) > 0
      error_message = "fargate_subnet_ids and fargate_security_group_ids are required when enable_fargate_browser_worker is true."
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.browser_worker_execution,
    aws_iam_role_policy.browser_worker_task,
  ]
}
