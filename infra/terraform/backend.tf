terraform {
  required_version = ">= 1.10"

  # S3 state with S3-native locking (no DynamoDB table needed).
  # bucket and region are passed at init time so nothing account-specific is
  # committed:
  #   terraform init -backend-config="bucket=pob-tfstate-<suffix>" -backend-config="region=ap-south-1"
  # scripts/setup.sh does this for you (and creates the bucket if missing).
  backend "s3" {
    key          = "proofofbalance/terraform.tfstate"
    use_lockfile = true
    encrypt      = true
  }
}
