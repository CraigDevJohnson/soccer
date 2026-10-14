variable "account_id" {
  description = "The workloads account, where the backend runs. Applies against any other account are refused."
  type        = string
  default     = "793680745829"
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be a 12-digit AWS account id."
  }
}

variable "region" {
  description = "The home Region. Everything of Craig's runs in us-west-2."
  type        = string
  default     = "us-west-2"
}

variable "state_bucket_name" {
  description = "The site's state bucket (the-lobby infra/bootstrap), where each deployment keeps its state under soccer/<environment>/."
  type        = string
  default     = "craigdevjohnson-tofu-state-793680745829"
}

variable "github_subject_prefix" {
  description = "Start of the sub claim in this repository's GitHub OIDC tokens. The repository uses GitHub's immutable form, with the owner's and the repository's ids, so a renamed or re-created repository can never match. Read the setting with: gh api repos/CraigDevJohnson/soccer/actions/oidc/customization/sub"
  type        = string
  default     = "repo:CraigDevJohnson@42454849/soccer@1412575682"
  validation {
    condition     = can(regex("^repo:[^*?:]+$", var.github_subject_prefix))
    error_message = "github_subject_prefix must be repo:<owner>/<repository> in one of GitHub's forms, with no wildcard."
  }
}
