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
