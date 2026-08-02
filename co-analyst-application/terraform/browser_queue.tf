resource "aws_sqs_queue" "browser_jobs_dlq" {
  name                      = "${local.name}-browser-jobs-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true

  tags = { Name = "${local.name}-browser-jobs-dlq" }
}

resource "aws_sqs_queue" "browser_jobs" {
  name                       = "${local.name}-browser-jobs"
  visibility_timeout_seconds = var.browser_worker_visibility_timeout_seconds
  message_retention_seconds  = 1209600
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.browser_jobs_dlq.arn
    maxReceiveCount     = 4
  })

  tags = { Name = "${local.name}-browser-jobs" }
}

resource "aws_sqs_queue_redrive_allow_policy" "browser_jobs" {
  queue_url = aws_sqs_queue.browser_jobs_dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.browser_jobs.arn]
  })
}
