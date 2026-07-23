terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.80.0, < 7.0.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Environment = "NonProd"
      Name        = "EDO-CoAnalyst-tool"
      contact     = "askdevopscloud@spglobal.com"
      AppID       = "ASP0017650"
      CreatedBy   = "Abhay.Lunkad"
      Owner       = "anuthama.c@spglobal.com"
    }
  }
}

# Read the current account and region so we can construct ARNs and image URIs
# without hard-coding IDs anywhere.
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
