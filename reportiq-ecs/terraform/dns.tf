# ════════════════════════════════════════════════════════════════════════════
#  Route53 — internal alias record for the ALB
# ════════════════════════════════════════════════════════════════════════════
resource "aws_route53_record" "app" {
  zone_id = var.hosted_zone_id
  name    = var.dns_name
  type    = "A"

  alias {
    name                   = aws_lb.app.dns_name
    zone_id                = aws_lb.app.zone_id
    evaluate_target_health = true
  }
}

import {
  to = aws_route53_record.app
  id = "${var.hosted_zone_id}_${var.dns_name}_A"
}
