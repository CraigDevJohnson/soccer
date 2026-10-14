terraform {
  required_version = ">= 1.13.0"

  # Each deployment's bucket and key are in environments/<env>.tfbackend,
  # which `task infra:init` (`ENV=prod` for production) passes in.
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
