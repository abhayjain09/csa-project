output "ecr_repository_url" {
  description = "ECR repo URL — push your image here"
  value       = aws_ecr_repository.app.repository_url
}

output "alb_dns_name" {
  description = "Internal ALB DNS name"
  value       = aws_lb.app.dns_name
}

output "portal_url" {
  description = "Internal URL for the app"
  value       = "https://${var.dns_name}"
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service" {
  value = aws_ecs_service.app.name
}

output "target_group_arn" {
  value = aws_lb_target_group.app.arn
}

output "log_group" {
  value = aws_cloudwatch_log_group.app.name
}

output "certificate_arn" {
  value = aws_acm_certificate.cert.arn
}
