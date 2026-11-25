locals {
  routine_roles = toset(var.site_environment == "prod" ? ["infra", "infra-plan"] : ["infra"])
  state_bucket  = "arn:aws:s3:::craigdevjohnson-tofu-state-${var.account_id}"
  state_prefix  = "soccer-archive/${var.site_environment}"
  state_object  = "${local.state_bucket}/${local.state_prefix}/terraform.tfstate"
  worker_logs   = "arn:aws:logs:us-west-2:${var.account_id}:log-group:/aws/lambda/${local.worker_name}"
  worker_alarm  = "arn:aws:cloudwatch:us-west-2:${var.account_id}:alarm:${local.worker_name}-*"
  worker_repo   = "arn:aws:ecr:us-west-2:${var.account_id}:repository/${local.worker_name}"
  scheduler     = "arn:aws:scheduler:us-west-2:${var.account_id}"
}

# The first, operator-approved apply creates these identities. The production
# preview can read its state and resource configuration but cannot lock, apply or write.
resource "aws_iam_role" "routine" {
  for_each = local.routine_roles
  name     = "${local.prefix}-${each.key}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = "arn:aws:iam::${var.account_id}:oidc-provider/${local.oidc_host}" }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_host}:aud" = "sts.amazonaws.com"
          "${local.oidc_host}:sub" = "${local.oidc_owner}archive-${each.key}-${var.site_environment}"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "routine_reads" {
  for_each = local.routine_roles
  name     = "read-archive-plan"
  role     = aws_iam_role.routine[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ListOwnStateWorkspace"
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = local.state_bucket
        Condition = { StringLike = { "s3:prefix" = "${local.state_prefix}/*" } }
      },
      {
        Sid      = "ReadOnlyOwnState"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = local.state_object
      },
      {
        Sid      = "ReadBucketConfiguration"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucket*", "s3:GetAccelerateConfiguration", "s3:GetEncryptionConfiguration", "s3:GetLifecycleConfiguration", "s3:GetReplicationConfiguration", "s3:ListTagsForResource"]
        Resource = concat([local.bucket_arn], local.keep_recovery ? [local.recovery_arn] : [])
      },
      {
        Sid      = "ReadArchiveRoles"
        Effect   = "Allow"
        Action   = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:ListRoleTags"]
        Resource = "arn:aws:iam::${var.account_id}:role/${local.prefix}-*"
      },
      {
        Sid      = "ReadWorker"
        Effect   = "Allow"
        Action   = ["lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:GetFunctionEventInvokeConfig", "lambda:GetFunctionCodeSigningConfig", "lambda:GetFunctionConcurrency", "lambda:ListVersionsByFunction", "lambda:ListTags"]
        Resource = local.worker_arn
      },
      {
        Sid      = "ReadWorkerImage"
        Effect   = "Allow"
        Action   = ["ecr:DescribeRepositories", "ecr:GetRepositoryPolicy", "ecr:ListTagsForResource", "ecr:DescribeImages"]
        Resource = local.worker_repo
      },
      {
        Sid      = "ReadSchedule"
        Effect   = "Allow"
        Action   = ["scheduler:GetSchedule", "scheduler:GetScheduleGroup", "scheduler:ListTagsForResource"]
        Resource = ["${local.scheduler}:schedule/${local.prefix}/${local.daily.Name}", "${local.scheduler}:schedule-group/${local.prefix}"]
      },
      {
        Sid       = "ReadRegionalMonitoringMetadata"
        Effect    = "Allow"
        Action    = ["logs:DescribeLogGroups", "cloudwatch:DescribeAlarms"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = "us-west-2" } }
      },
      {
        Sid      = "ReadWorkerLogConfiguration"
        Effect   = "Allow"
        Action   = ["logs:DescribeMetricFilters", "logs:ListTagsForResource"]
        Resource = [local.worker_logs, "${local.worker_logs}:*"]
      },
      {
        Sid      = "ReadWorkerAlarmConfiguration"
        Effect   = "Allow"
        Action   = ["cloudwatch:ListTagsForResource"]
        Resource = local.worker_alarm
      }
    ]
  })
}

resource "aws_iam_role_policy" "routine_writes" {
  name = "apply-archive-plan"
  role = aws_iam_role.routine["infra"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "SaveOwnState"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = local.state_object
      },
      {
        Sid      = "AcquireOwnStateLock"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
        Resource = "${local.state_object}.tflock"
      },
      {
        Sid      = "ReadWorkerReleaseGate"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = [for key in local.handoff_keys : "${local.bucket_arn}/${key}"]
      },
      {
        Sid      = "ManagePrimaryLifecycle"
        Effect   = "Allow"
        Action   = ["s3:PutLifecycleConfiguration"]
        Resource = local.bucket_arn
      },
      {
        Sid      = "UpdateWorker"
        Effect   = "Allow"
        Action   = ["lambda:UpdateFunctionConfiguration", "lambda:UpdateFunctionCode", "lambda:PutFunctionConcurrency", "lambda:PutFunctionEventInvokeConfig", "lambda:UpdateFunctionEventInvokeConfig", "lambda:TagResource", "lambda:UntagResource"]
        Resource = local.worker_arn
      },
      {
        Sid      = "PullWorkerImage"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = local.worker_repo
      },
      {
        Sid      = "ManageWorkerMonitoring"
        Effect   = "Allow"
        Action   = ["logs:PutMetricFilter", "logs:PutRetentionPolicy", "logs:TagResource", "logs:UntagResource"]
        Resource = [local.worker_logs, "${local.worker_logs}:*"]
      },
      {
        Sid       = "AlarmsNotifyOnlyAlerts"
        Effect    = "Allow"
        Action    = ["cloudwatch:PutMetricAlarm"]
        Resource  = local.worker_alarm
        Condition = { "ForAllValues:StringEquals" = { "cloudwatch:AlarmActions" = [local.alerts] } }
      },
      {
        Sid      = "TagWorkerAlarms"
        Effect   = "Allow"
        Action   = ["cloudwatch:TagResource", "cloudwatch:UntagResource"]
        Resource = local.worker_alarm
      },
      {
        Sid      = "ConfigureOwnSchedule"
        Effect   = "Allow"
        Action   = ["scheduler:UpdateSchedule"]
        Resource = "${local.scheduler}:schedule/${local.prefix}/${local.daily.Name}"
      },
      {
        Sid       = "PassOnlySchedulerRole"
        Effect    = "Allow"
        Action    = ["iam:PassRole"]
        Resource  = "arn:aws:iam::${var.account_id}:role/${local.prefix}-scheduler"
        Condition = { StringEquals = { "iam:PassedToService" = "scheduler.amazonaws.com" } }
      }
    ]
  })
}
