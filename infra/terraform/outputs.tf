output "elastic_ip" {
  description = "Point the dashboard's A record here unless route53_zone_id is set."
  value       = aws_eip.app.public_ip
}

output "instance_id" {
  description = "Repository variable AWS_INSTANCE_ID — the deploy workflow sends deploy.sh here through SSM."
  value       = aws_instance.app.id
}

output "backup_bucket" {
  description = "Repository variable RELEASE_BUCKET — nightly backups and the release files."
  value       = aws_s3_bucket.backups.bucket
}

output "deploy_role_arn" {
  description = "Repository variable AWS_DEPLOY_ROLE_ARN."
  value       = aws_iam_role.github_deploy.arn
}

output "terraform_role_arn" {
  description = "Repository variable AWS_TERRAFORM_ROLE_ARN."
  value       = aws_iam_role.github_terraform.arn
}

output "session" {
  description = "A shell on the box (no SSH port): requires the Session Manager plugin for the AWS CLI."
  value       = "aws ssm start-session --region ${var.region} --target ${aws_instance.app.id}"
}
