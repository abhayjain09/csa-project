############################################
# DynamoDB tables for RAG indexing + Q&A pipeline
# Project: EDO Co-Analyst / Report IQ (AppID ASP0017650)
############################################

locals {
  common_tags = {
    Environment = "NonProd"
    AppID       = "ASP0017650"
    CreatedBy   = "Abhay.Lunkad"
    Owner       = "anuthama.c@spglobal.com"
    contact     = "askdevopscloud@spglobal.com"
  }
}

############################################
# 1) pageindex-runs
# Tracks page-level indexing jobs for a stored document
# (chunking + embedding a PDF already discovered/stored by
# the report-retrieval agent, keyed per company/document run).
############################################
resource "aws_dynamodb_table" "pageindex_runs" {
  name         = "pageindex-runs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "company"
  range_key    = "run_id"

  attribute {
    name = "company"
    type = "S"
  }

  attribute {
    name = "run_id"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "started_at"
    type = "S"
  }

  # Query across all companies for runs in a given status
  # (e.g. find every "failed" or "in_progress" indexing run)
  global_secondary_index {
    name            = "status-started_at-index"
    hash_key        = "status"
    range_key       = "started_at"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name = "pageindex-runs"
  })
}

############################################
# 2) answering-runs
# Tracks each end-to-end Q&A invocation: one user question
# (in a session) triggers one agent run through retrieval + generation.
############################################
resource "aws_dynamodb_table" "answering_runs" {
  name         = "answering-runs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "session_id"
  range_key    = "run_id"

  attribute {
    name = "session_id"
    type = "S"
  }

  attribute {
    name = "run_id"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  # Monitor runs by status across all sessions (e.g. dashboard
  # of currently running / failed / completed Q&A invocations)
  global_secondary_index {
    name            = "status-created_at-index"
    hash_key        = "status"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name = "answering-runs"
  })
}

############################################
# 3) answering-results
# Stores the final answer + citations/evidence for a given run.
# Allows multiple result rows per run (e.g. retries, revised answers,
# multiple candidate answers before final selection).
############################################
resource "aws_dynamodb_table" "answering_results" {
  name         = "answering-results"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"
  range_key    = "result_id"

  attribute {
    name = "run_id"
    type = "S"
  }

  attribute {
    name = "result_id"
    type = "S"
  }

  attribute {
    name = "company"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  # Browse answer history per company over time
  global_secondary_index {
    name            = "company-created_at-index"
    hash_key        = "company"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name = "answering-results"
  })
}

############################################
# Outputs
############################################
output "pageindex_runs_table_arn" {
  value = aws_dynamodb_table.pageindex_runs.arn
}

output "answering_runs_table_arn" {
  value = aws_dynamodb_table.answering_runs.arn
}

output "answering_results_table_arn" {
  value = aws_dynamodb_table.answering_results.arn
}
