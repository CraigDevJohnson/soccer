variable "account_id" {
  description = "The AWS account the backend runs in (the workloads account)."
  type        = string
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

variable "resource_prefix" {
  description = "First part of every resource name."
  type        = string
  default     = "soccer"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,19}$", var.resource_prefix))
    error_message = "resource_prefix must be 1 to 20 lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "site_environment" {
  description = "The calling site's environment, such as dev or prod. It goes in the function's name (<resource_prefix>-<site_environment>-server), because the site's permissions boundary lets it call only backends named *-<its environment>-*."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9]{0,11}$", var.site_environment))
    error_message = "site_environment must be up to 12 lowercase letters and digits, starting with a letter."
  }
}

variable "site_role_arn" {
  description = "The site's own Lambda role (site-<environment>-server in the-lobby), the only principal allowed to call the Function URL (the-lobby ADR 0006)."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:role/.+$", var.site_role_arn)) && split(":", var.site_role_arn)[4] == var.account_id
    error_message = "site_role_arn must name an IAM role in the same account as the backend."
  }
}

variable "package_path" {
  description = "The zip built by `task lambda`: one static arm64 binary named bootstrap. The tasks pass ../dist/soccer.zip, relative to infra/, so the path kept in state is the same from every checkout."
  type        = string
}

variable "link_base_url" {
  description = "Where the site serves Session links: its address followed by /soccer/link/. The backend appends each link's token to it."
  type        = string
  validation {
    condition     = startswith(var.link_base_url, "https://") && endswith(var.link_base_url, "/soccer/link/")
    error_message = "link_base_url must be the site's https address followed by /soccer/link/, such as https://site.example.com/soccer/link/."
  }
}

variable "league_base_url" {
  description = "The league's schedule service. Leave unset for the one the code knows (README)."
  type        = string
  default     = null
  validation {
    condition     = var.league_base_url == null || can(regex("^https://[^/]+$", var.league_base_url))
    error_message = "league_base_url must be an https origin with no path, or unset."
  }
}

variable "league_facility_id" {
  description = "The Facility in the league's GET /facilities list. Leave unset for Boise, the code's default."
  type        = string
  default     = null
  validation {
    condition     = var.league_facility_id == null || can(regex("^[0-9]+$", var.league_facility_id))
    error_message = "league_facility_id must be a number, or unset."
  }
}

variable "alert_topic_arn" {
  description = "The SNS topic the alarms notify: the workloads account's us-west-2 alerts topic, which already allows CloudWatch to publish and emails Craig (aws-design.md section 7)."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:sns:[a-z0-9-]+:[0-9]{12}:.+$", var.alert_topic_arn)) && split(":", var.alert_topic_arn)[3] == var.region && split(":", var.alert_topic_arn)[4] == var.account_id
    error_message = "alert_topic_arn must name an SNS topic in the backend's account and Region."
  }
}

variable "permissions_boundary_arn" {
  description = "Permissions boundary for the function's role: soccer-server-boundary-<site_environment> from infra/bootstrap. The deploy role may only create or change the role while it carries that boundary, so every deployment in infra/environments sets it. Unset only for a deployment made with the admin profile alone."
  type        = string
  default     = null
  validation {
    condition     = var.permissions_boundary_arn == null || (can(regex("^arn:aws:iam::[0-9]{12}:policy/.+$", var.permissions_boundary_arn)) && try(split(":", var.permissions_boundary_arn)[4] == var.account_id, false))
    error_message = "permissions_boundary_arn must name an IAM policy in the same account as the backend, or be unset."
  }
}

variable "log_retention_days" {
  description = "How long the function's log group keeps lines. 30 days is the default retention in aws-design.md (G8)."
  type        = number
  default     = 30
  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365], var.log_retention_days)
    error_message = "log_retention_days must be one of the retention periods CloudWatch Logs offers."
  }
}

variable "tags" {
  description = "Extra tags on every resource, such as the site's cost tag. The fixed tags (project, Environment, ManagedBy, Repo) cannot be overridden."
  type        = map(string)
  default     = {}
  validation {
    condition     = length(setintersection(keys(var.tags), ["project", "Environment", "ManagedBy", "Repo"])) == 0
    error_message = "tags may not name project, Environment, ManagedBy or Repo; the root sets those itself."
  }
}
