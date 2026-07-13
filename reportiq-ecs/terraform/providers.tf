terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}

provider "aws" {
  region = var.region

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

provider "tls" {}
