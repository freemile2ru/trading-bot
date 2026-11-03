# The dashboard's A record, when the domain's zone is in Route 53. Otherwise point the name at `elastic_ip` yourself.
resource "aws_route53_record" "app" {
  count = var.route53_zone_id == "" ? 0 : 1

  zone_id = var.route53_zone_id
  name    = var.domain
  type    = "A"
  ttl     = 300
  records = [aws_eip.app.public_ip]
}
