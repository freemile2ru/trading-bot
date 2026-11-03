# Secrets live in SSM Parameter Store, never in Terraform state or GitHub. Terraform creates each parameter with a
# placeholder and then ignores its value; you set the real values once with the AWS CLI (deploy/README.md).
#
#   /ai-trader/app-env     the backend's .env, verbatim (ANTHROPIC_API_KEY=..., BINANCE_*=..., ...)
#   /ai-trader/web-env     DOMAIN=..., BASIC_AUTH_USER=..., BASIC_AUTH_HASH=... (bcrypt from `caddy hash-password`)
#   /ai-trader/ghcr-user   the GitHub user whose token pulls the private images
#   /ai-trader/ghcr-token  a GitHub token with read:packages
locals {
  secret_parameters = ["app-env", "web-env", "ghcr-user", "ghcr-token"]
}

resource "aws_ssm_parameter" "secret" {
  for_each = toset(local.secret_parameters)

  name  = "/ai-trader/${each.key}"
  type  = "SecureString"
  value = "REPLACE-ME"

  lifecycle {
    ignore_changes = [value]
  }
}
