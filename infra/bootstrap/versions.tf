terraform {
  required_version = ">= 1.13.0"

  # The deploy roles' own state, beside the deployments' keys in the site's
  # state bucket. Nothing here is private, so the settings are committed.
  backend "s3" {
    bucket       = "craigdevjohnson-tofu-state-793680745829"
    key          = "soccer/bootstrap/terraform.tfstate"
    region       = "us-west-2"
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
