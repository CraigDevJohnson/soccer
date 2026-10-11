terraform {
  required_version = ">= 1.13.0"

  # The bucket and key are private (docs/operations/deploy.md): copy
  # backend.hcl.example to backend.<env>.hcl beside this file (backend.dev.hcl
  # for the dev site, backend.prod.hcl for production), then `task infra:init`
  # (`ENV=prod` for production).
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
