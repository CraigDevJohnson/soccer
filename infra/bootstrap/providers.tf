# Credentials come from AWS_PROFILE set to the workloads account's admin
# profile after `aws sso login`: these roles are what CI signs in as, so
# they cannot be made by CI (docs/operations/deploy.md, "One-time setup").
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = {
      project     = "soccer"
      Environment = "shared"
      ManagedBy   = "opentofu"
      Repo        = "soccer"
    }
  }
}
