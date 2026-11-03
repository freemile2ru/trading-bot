#!/usr/bin/env bash
# One-time, from your own AWS credentials: the Terraform state bucket and lock table (Terraform cannot create its
# own backend). Prints the two repository variables to set.
#
#   infra/bootstrap.sh            # region ap-northeast-1
#   infra/bootstrap.sh eu-central-1
set -euo pipefail
REGION="${1:-ap-northeast-1}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="ai-trader-tfstate-$ACCOUNT"
TABLE="ai-trader-tflock"

if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" --create-bucket-configuration LocationConstraint="$REGION"
  aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
  aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
fi
if ! aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" >/dev/null 2>&1; then
  aws dynamodb create-table --table-name "$TABLE" --region "$REGION" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH --billing-mode PAY_PER_REQUEST >/dev/null
fi

cat <<EOF

State bucket: $BUCKET   lock table: $TABLE   region: $REGION

GitHub → Settings → Secrets and variables → Actions → Variables:
  TF_STATE_BUCKET = $BUCKET
  TF_LOCK_TABLE   = $TABLE
  AWS_REGION      = $REGION

First apply (from this machine, before the GitHub roles exist):
  cd infra/terraform
  terraform init -backend-config="bucket=$BUCKET" -backend-config="dynamodb_table=$TABLE" -backend-config="region=$REGION"
  terraform apply -var domain=YOUR.DOMAIN
EOF
