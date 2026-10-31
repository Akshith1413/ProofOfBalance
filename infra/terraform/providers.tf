terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region

  # Every resource is tagged so costs are traceable in Cost Explorer, and so
  # scripts/teardown.sh can find stragglers if the state is ever lost.
  default_tags {
    tags = {
      Project     = "ProofOfBalance"
      Owner       = var.owner
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
