terraform {
  required_version = ">= 1.13.0"
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

provider "aws" {
  region              = "us-west-2"
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = {
      project     = "soccer"
      Environment = var.site_environment
      ManagedBy   = "opentofu"
      Repo        = "soccer"
      Site        = "craigdevjohnson.com"
    }
  }
}

variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Expected the 12-digit workloads account ID."
  }
}

variable "site_environment" {
  type = string
  validation {
    condition     = contains(["dev", "prod"], var.site_environment)
    error_message = "Choose dev or prod."
  }
}

variable "image_digest" {
  type        = string
  default     = null
  description = "Reviewed worker image digest in this environment's ECR repository. Set it in the environment tfvars for worker creation, subsequent releases and replacement."
  validation {
    condition     = var.image_digest == null || can(regex("^sha256:[0-9a-f]{64}$", var.image_digest))
    error_message = "Specify an immutable sha256 image digest."
  }
}

variable "architecture" {
  type    = string
  default = "arm64"
  validation {
    condition     = var.architecture == "arm64"
    error_message = "The archive worker image is built for arm64."
  }
}

variable "staging_access" {
  type        = object({ principal_arn = string, generation = string })
  default     = null
  description = "Optional, temporary access from an exact same-account Identity Center role to the two files of one approved local baseline."
  validation {
    condition = var.staging_access == null ? true : (
      can(regex("^arn:aws:iam::${var.account_id}:role/aws-reserved/sso\\.amazonaws\\.com/([a-z0-9-]+/)?AWSReservedSSO_[A-Za-z0-9+=,.@_-]+_[0-9a-fA-F]{16}$", var.staging_access.principal_arn))
      && can(regex("^[a-f0-9]{64}$", var.staging_access.generation))
    )
    error_message = "Supply the exact same-account Identity Center role ARN and the generation's SHA-256."
  }
}

locals {
  daily          = jsondecode(file("${path.module}/daily.json"))
  settings_key   = "settings/collection.json"
  prefix         = "soccer-archive-${var.site_environment}"
  worker_name    = "${local.prefix}-worker"
  worker_arn     = "arn:aws:lambda:us-west-2:${var.account_id}:function:${local.worker_name}"
  dev_worker_arn = "arn:aws:lambda:us-west-2:${var.account_id}:function:soccer-archive-dev-worker"
  dev_repo_arn   = "arn:aws:ecr:us-west-2:${var.account_id}:repository/soccer-archive-dev-worker"
  alerts         = "arn:aws:sns:us-west-2:${var.account_id}:alerts"
  bucket_name    = "${local.prefix}-${var.account_id}"
  bucket_arn     = "arn:aws:s3:::${local.bucket_name}"
  peer_env       = var.site_environment == "dev" ? "prod" : "dev"
  peer_bucket    = "arn:aws:s3:::soccer-archive-${local.peer_env}-${var.account_id}"
  handoff_keys   = [local.settings_key, "control/worker.json"]
  worker_enabled = var.image_digest != null
}

output "archive_bucket" { value = aws_s3_bucket.archive.bucket }
output "worker_repository_url" { value = aws_ecr_repository.worker.repository_url }
output "worker_function_name" { value = one(aws_lambda_function.worker[*].function_name) }
output "image_publisher_role_arn" { value = aws_iam_role.github["image"].arn }
output "import_role_arn" { value = aws_iam_role.github["import"].arn }
output "activation_role_arn" { value = aws_iam_role.github["activation"].arn }
output "run_role_arn" { value = aws_iam_role.github["run"].arn }
output "staging_role_arn" { value = one(aws_iam_role.stage[*].arn) }
