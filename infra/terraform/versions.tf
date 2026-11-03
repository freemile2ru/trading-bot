terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }

  # The state bucket and lock table are created once by infra/bootstrap.sh; the workflow passes their names with
  # -backend-config (a backend block cannot read variables).
  backend "s3" {
    key     = "ai-trader/terraform.tfstate"
    encrypt = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = { Project = "ai-trader", ManagedBy = "terraform" }
  }
}
