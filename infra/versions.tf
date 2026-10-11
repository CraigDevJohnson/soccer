terraform {
  required_version = ">= 1.13.0"

  # The bucket and key are private (docs/operations/deploy.md): copy
  # backend.hcl.example to backend.hcl beside this file, then `task infra:init`.
  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}
