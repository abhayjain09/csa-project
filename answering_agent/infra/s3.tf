# ---------------------------------------------------------------------------
# Optional S3 bucket to hold the pageindex JSON and questionnaire MD files.
#
# Skipped entirely when var.create_input_bucket = false — in that case the
# runtime's IAM policy references the pre-existing bucket you name in
# var.existing_input_bucket_name.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "input" {
  count = var.create_input_bucket ? 1 : 0

  bucket = "${var.project_name}-${var.environment}-inputs-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "input" {
  count = var.create_input_bucket ? 1 : 0

  bucket                  = aws_s3_bucket.input[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "input" {
  count = var.create_input_bucket ? 1 : 0

  bucket = aws_s3_bucket.input[0].id
  versioning_configuration {
    # Versioning on so we can point the runtime at older pageindex snapshots
    # if a rebuild introduces regressions.
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "input" {
  count = var.create_input_bucket ? 1 : 0

  bucket = aws_s3_bucket.input[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}
