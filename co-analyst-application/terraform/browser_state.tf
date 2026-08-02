resource "aws_s3_bucket" "browser_state" {
  bucket = "${local.name}-browser-state-${local.acct}-${local.region}"

  tags = { Name = "${local.name}-browser-state" }
}

resource "aws_s3_bucket_public_access_block" "browser_state" {
  bucket = aws_s3_bucket.browser_state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "browser_state" {
  bucket = aws_s3_bucket.browser_state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "browser_state" {
  bucket = aws_s3_bucket.browser_state.id

  rule {
    id     = "expire-browser-session-state"
    status = "Enabled"

    filter {}

    expiration {
      days = 7
    }
  }
}

data "aws_iam_policy_document" "browser_state_tls" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.browser_state.arn,
      "${aws_s3_bucket.browser_state.arn}/*",
    ]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "browser_state_tls" {
  bucket = aws_s3_bucket.browser_state.id
  policy = data.aws_iam_policy_document.browser_state_tls.json
}
