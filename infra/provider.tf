# Credentials come from the environment: AWS_PROFILE set to the workloads
# account's admin profile after `aws sso login` (aws-setup, aws-design.md).
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = merge({
      project     = "soccer"
      Environment = var.site_environment
      ManagedBy   = "opentofu"
      Repo        = "soccer"
    }, var.tags)
  }
}
