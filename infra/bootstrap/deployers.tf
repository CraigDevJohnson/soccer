# What the Deploy workflow may do in the workloads account.
#
# GitHub signs a short-lived OpenID Connect token for each job. AWS trusts
# that token through the account's GitHub provider and hands a deploy job
# temporary credentials for its environment's role, so no AWS key is stored
# in GitHub (aws-design.md section 5.6, G2).

# aws-setup owns the provider (aws-design.md section 10); this root only
# looks it up.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://${local.github_oidc_host}"
}

locals {
  # Per deployment, the names its root gives everything it makes, and the
  # ARNs the deploy role's policy is pinned to.
  deployments = {
    for env, settings in local.environments : env => merge(settings, {
      name_prefix  = "soccer-${env}"
      state_prefix = "soccer/${env}"
      subject      = "${var.github_subject_prefix}:environment:${settings.github_environment}"

      functions = [
        "arn:aws:lambda:${var.region}:${var.account_id}:function:soccer-${env}-*",
        "arn:aws:lambda:${var.region}:${var.account_id}:function:soccer-${env}-*:*",
      ]
      roles         = "arn:aws:iam::${var.account_id}:role/soccer-${env}-*"
      server_role   = "arn:aws:iam::${var.account_id}:role/soccer-${env}-server"
      log_groups    = "arn:aws:logs:${var.region}:${var.account_id}:log-group:/aws/lambda/soccer-${env}-*"
      alarms        = "arn:aws:cloudwatch:${var.region}:${var.account_id}:alarm:soccer-${env}-*"
      site_role_arn = "arn:aws:iam::${var.account_id}:role/${settings.site_role}"
    })
  }

  state_bucket_arn = "arn:aws:s3:::${var.state_bucket_name}"

  # The account's alerts topic, the one place a soccer alarm may notify
  # (alert_topic_arn in infra/environments/<env>.tfvars).
  alert_topic_arn = "arn:aws:sns:${var.region}:${var.account_id}:alerts"
}

# ---------------------------------------------------------------------------
# The most a soccer function's role may ever do, whatever its own policy
# says: write its own log group. One per deployment, so a dev role can never
# be given a policy that reaches production's logs, and the deploy role can
# only change a role that carries its deployment's boundary.

resource "aws_iam_policy" "server_boundary" {
  for_each = local.deployments

  name        = "soccer-server-boundary-${each.key}"
  description = "Permissions boundary for the ${each.key} soccer backend roles (${each.value.name_prefix}-*): their own logs only."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "OwnLogsOnly"
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = ["${each.value.log_groups}:*"]
    }]
  })
}

# ---------------------------------------------------------------------------
# Deploy roles: one per deployment. Each trusts only the token of a job that
# targets its GitHub environment in this repository, with the exact aud and
# sub (StringEquals, no wildcards).

resource "aws_iam_role" "deployer" {
  for_each = local.deployments

  # Outside soccer-<env>-*, so a deploy role can never change itself.
  name                 = "soccer-deployer-${each.key}"
  description          = "GitHub Actions deploys of the soccer backend to ${each.key} (GitHub environment ${each.value.github_environment})."
  max_session_duration = 3600

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "GitHubActionsOidc"
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.github_oidc_host}:aud" = "sts.amazonaws.com"
          "${local.github_oidc_host}:sub" = each.value.subject
        }
      }
    }]
  })
}

# Everything an apply of infra/ needs, and no more: its state, the function,
# Function URL, permissions, log group, metric filter and alarms named
# soccer-<env>-*, and the function's role, soccer-<env>-server. Reads are
# listed per service and pinned to the same names wherever the action allows
# it, so this role cannot read another app's state, function settings or
# secrets. Four conditions keep the backend private and the role inside its
# own deployment whatever a commit says: the Function URL can only be
# AWS_IAM, a call may only be granted to the site's role, an alarm may only
# notify the alerts topic (never stop or terminate an instance, which an
# alarm on another app's metric could), and the function's role may only be
# given a policy or a boundary inside the boundary above, which stays on.
#
# IAM has no condition on what a trust policy says, so this role never sets
# one: it cannot make the function's role (CreateRole), change its trust
# (UpdateAssumeRolePolicy) or session length (UpdateRole), or delete it
# (DeleteRole). A deployment's first apply, from a computer, makes the role.
# The Deploy workflow refuses a plan that would make, replace or remove the
# role or change its trust, so such a commit is applied from a computer
# instead (docs/operations/deploy.md).
resource "aws_iam_role_policy" "deployer" {
  for_each = local.deployments

  name = "deploy-${each.key}"
  role = aws_iam_role.deployer[each.key].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # OpenTofu state: this deployment's key and its lock file
      # (<key>.tflock), nothing under another deployment's prefix. The S3
      # backend lists the workspaces on every init, plan, apply and output;
      # each tfbackend sets workspace_key_prefix to soccer/<env>, so that list
      # stays inside the prefix too. With the listing pinned, S3 answers 403
      # rather than 404 for a key that does not exist yet, so a new
      # deployment's first apply, which writes its state key, runs from a
      # computer.
      {
        Sid      = "StateList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [local.state_bucket_arn]
        Condition = {
          StringLike = { "s3:prefix" = ["${each.value.state_prefix}/*"] }
        }
      },
      {
        Sid      = "StateReadWrite"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = ["${local.state_bucket_arn}/${each.value.state_prefix}/*"]
      },

      # Lambda: the function, its configuration and tags, and reading it back.
      {
        Sid    = "Function"
        Effect = "Allow"
        Action = [
          "lambda:CreateFunction",
          "lambda:UpdateFunctionCode",
          "lambda:UpdateFunctionConfiguration",
          "lambda:DeleteFunction",
          "lambda:DeleteFunctionUrlConfig",
          "lambda:RemovePermission",
          "lambda:TagResource",
          "lambda:UntagResource",
          "lambda:Get*",
          "lambda:List*",
        ]
        Resource = each.value.functions
      },
      # A create always names the auth type; an update names it only when it
      # changes it, and then it must still be AWS_IAM.
      {
        Sid      = "FunctionUrlIamOnly"
        Effect   = "Allow"
        Action   = ["lambda:CreateFunctionUrlConfig", "lambda:UpdateFunctionUrlConfig"]
        Resource = each.value.functions
        Condition = {
          StringEqualsIfExists = { "lambda:FunctionUrlAuthType" = "AWS_IAM" }
        }
      },
      {
        Sid      = "InvokeGrantsToTheSiteOnly"
        Effect   = "Allow"
        Action   = ["lambda:AddPermission"]
        Resource = each.value.functions
        Condition = {
          StringEquals = { "lambda:Principal" = each.value.site_role_arn }
        }
      },

      # CloudWatch Logs: the function's log group, its retention, tags and
      # metric filter. The provider reads the metric filter by its log
      # group's name (DescribeMetricFilters with logGroupName), so that read
      # is pinned to the same log groups.
      {
        Sid    = "LogGroup"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:DeleteLogGroup",
          "logs:PutRetentionPolicy",
          "logs:DeleteRetentionPolicy",
          "logs:PutMetricFilter",
          "logs:DeleteMetricFilter",
          "logs:DescribeMetricFilters",
          "logs:TagResource",
          "logs:UntagResource",
          "logs:ListTagsForResource",
        ]
        Resource = [each.value.log_groups]
      },
      # Describe calls return names and settings, never log lines.
      # DescribeLogGroups takes no resource, so AWS documents it as needing
      # "*"; it reaches no further than every log group in the Region.
      {
        Sid      = "LogGroupList"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = ["*"]
      },

      # CloudWatch alarms, named soccer-<env>-*. AWS documents
      # cloudwatch:AlarmActions as the actions an alarm may take when its
      # state changes; an alarm with none is allowed. That it also covers an
      # alarm's OK and insufficient-data actions is what the Check alarm
      # guard workflow tests (docs/operations/deploy.md).
      {
        Sid      = "AlarmsNotifyOnlyAlerts"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricAlarm"]
        Resource = [each.value.alarms]
        Condition = {
          "ForAllValues:StringEquals" = { "cloudwatch:AlarmActions" = [local.alert_topic_arn] }
        }
      },
      {
        Sid    = "Alarms"
        Effect = "Allow"
        Action = [
          "cloudwatch:DeleteAlarms",
          "cloudwatch:TagResource",
          "cloudwatch:UntagResource",
          "cloudwatch:ListTagsForResource",
        ]
        Resource = [each.value.alarms]
      },
      # Alarm settings hold no secrets. Granted on every alarm because a
      # refused read would fail every plan, and whether AWS honours a
      # soccer-<env>-* pin for the provider's lookup by name is unproven.
      {
        Sid      = "AlarmRead"
        Effect   = "Allow"
        Action   = ["cloudwatch:DescribeAlarms"]
        Resource = ["*"]
      },

      # IAM: the function's role and no other. Given a policy or a boundary
      # only while it carries this deployment's boundary; read, described and
      # tagged; handed to Lambda and to nothing else. Never made, given a new
      # trust or deleted (above).
      {
        Sid      = "RoleInsideBoundary"
        Effect   = "Allow"
        Action   = ["iam:PutRolePolicy", "iam:PutRolePermissionsBoundary"]
        Resource = [each.value.server_role]
        Condition = {
          StringEquals = { "iam:PermissionsBoundary" = aws_iam_policy.server_boundary[each.key].arn }
        }
      },
      {
        Sid    = "Role"
        Effect = "Allow"
        Action = [
          "iam:GetRole",
          "iam:GetRolePolicy",
          "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies",
          "iam:ListRoleTags",
          "iam:UpdateRoleDescription",
          "iam:DeleteRolePolicy",
          "iam:TagRole",
          "iam:UntagRole",
        ]
        Resource = [each.value.server_role]
      },
      {
        Sid      = "BoundaryStays"
        Effect   = "Deny"
        Action   = ["iam:DeleteRolePermissionsBoundary"]
        Resource = [each.value.roles]
      },
      {
        Sid      = "PassRoleToLambdaOnly"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [each.value.server_role]
        Condition = {
          StringEquals = { "iam:PassedToService" = "lambda.amazonaws.com" }
        }
      },
    ]
  })
}
