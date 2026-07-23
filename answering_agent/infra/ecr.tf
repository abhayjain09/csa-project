# ---------------------------------------------------------------------------
# ECR repository for the agent container image.
#
# The AgentCore runtime resource references an image URI in this repo.
# The image itself is built and pushed OUT-OF-BAND (via docker buildx or a
# CI pipeline) — Terraform doesn't build containers. See infra/README.md for
# the deploy sequence.
# ---------------------------------------------------------------------------

resource "aws_ecr_repository" "agent" {
  name                 = "${var.project_name}-${var.environment}"
  image_tag_mutability = "MUTABLE" # Set to IMMUTABLE if you tag by SHA in prod.

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# Lifecycle policy — cap image history to keep the repo bounded.
resource "aws_ecr_lifecycle_policy" "agent" {
  repository = aws_ecr_repository.agent.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Retain last 20 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 20
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}
