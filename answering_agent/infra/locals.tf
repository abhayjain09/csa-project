# Shared locals referenced across multiple .tf files.
#
# The ternary works because Terraform short-circuits reference resolution
# in conditional expressions — if create_input_bucket = false, the
# aws_s3_bucket.input[0] side is never evaluated even though count = 0
# would otherwise make that reference invalid.

locals {
  input_bucket_name = var.create_input_bucket ? aws_s3_bucket.input[0].id : var.existing_input_bucket_name
  input_bucket_arn  = "arn:aws:s3:::${local.input_bucket_name}"
  name_prefix       = "${var.project_name}-${var.environment}"
}

# Fail fast at plan time if existing bucket is chosen but not named.
check "input_bucket_config" {
  assert {
    condition     = var.create_input_bucket || length(var.existing_input_bucket_name) > 0
    error_message = "existing_input_bucket_name must be set when create_input_bucket = false."
  }
}
