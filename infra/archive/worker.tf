resource "aws_iam_role" "worker" {
  count = local.worker_enabled ? 1 : 0
  name  = local.worker_name
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_cloudwatch_log_group" "worker" {
  count             = local.worker_enabled ? 1 : 0
  name              = "/aws/lambda/${local.worker_name}"
  retention_in_days = 30
}

resource "aws_iam_role_policy" "worker" {
  count = local.worker_enabled ? 1 : 0
  role  = aws_iam_role.worker[0].id
  name  = "collect"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WorkerLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.worker[0].arn}:*"
      },
      {
        Sid      = "ArchiveObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = [for prefix in ["generations", "control", "journal"] : "${local.bucket_arn}/${prefix}/*"]
      },
      {
        Sid      = "ReadCollectionMode"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${local.bucket_arn}/${local.settings_key}"
      }
    ]
  })
}

# Verified tags are immutable and are published only from the dev worker's active digest.
data "aws_ecr_image" "promoted" {
  count           = local.worker_enabled && var.site_environment == "prod" ? 1 : 0
  repository_name = local.worker_name
  image_tag       = "verified-${trimprefix(var.image_digest, "sha256:")}"
}

resource "aws_lambda_function" "worker" {
  count                          = local.worker_enabled ? 1 : 0
  function_name                  = local.worker_name
  description                    = "Bounded Boise archive collection for ${var.site_environment}."
  package_type                   = "Image"
  image_uri                      = "${aws_ecr_repository.worker.repository_url}@${var.image_digest}"
  role                           = aws_iam_role.worker[0].arn
  architectures                  = [var.architecture]
  memory_size                    = 2048
  timeout                        = 900
  reserved_concurrent_executions = 1
  ephemeral_storage { size = 2048 }
  environment { variables = { ARCHIVE_BUCKET = aws_s3_bucket.archive.bucket } }
  lifecycle {
    precondition {
      condition     = var.site_environment == "prod" ? data.aws_ecr_image.promoted[0].image_digest == var.image_digest : true
      error_message = "Production requires the exact image promoted from dev under its immutable verified tag."
    }
  }
  depends_on = [
    aws_ecr_repository_policy.worker, aws_iam_role_policy.worker,
    aws_s3_bucket_policy.archive, aws_s3_bucket_versioning.archive,
    aws_s3_bucket_server_side_encryption_configuration.archive, aws_s3_bucket_replication_configuration.recovery,
  ]
}

resource "aws_lambda_function_event_invoke_config" "worker" {
  count                        = local.worker_enabled ? 1 : 0
  function_name                = aws_lambda_function.worker[0].function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 3600
}

resource "aws_cloudwatch_metric_alarm" "errors" {
  count               = local.worker_enabled ? 1 : 0
  alarm_name          = "${local.worker_name}-errors"
  alarm_description   = "The archive worker failed or timed out. Inspect its run and resume its remaining budget."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.worker[0].function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alerts]
  ok_actions          = [local.alerts]
}

resource "aws_scheduler_schedule_group" "archive" {
  count = local.worker_enabled ? 1 : 0
  name  = local.prefix
}

resource "aws_iam_role" "scheduler" {
  count = local.worker_enabled ? 1 : 0
  name  = "${local.prefix}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "scheduler.amazonaws.com" }
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = var.account_id
          "aws:SourceArn"     = aws_scheduler_schedule_group.archive[0].arn
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  count = local.worker_enabled ? 1 : 0
  name  = "invoke-worker"
  role  = aws_iam_role.scheduler[0].id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.worker[0].arn }]
  })
}

resource "aws_scheduler_schedule" "daily" {
  count                        = local.worker_enabled ? 1 : 0
  name                         = local.daily.Name
  description                  = local.daily.Description
  group_name                   = aws_scheduler_schedule_group.archive[0].name
  state                        = "DISABLED"
  action_after_completion      = local.daily.ActionAfterCompletion
  schedule_expression          = local.daily.ScheduleExpression
  schedule_expression_timezone = local.daily.ScheduleExpressionTimezone
  flexible_time_window { mode = local.daily.FlexibleTimeWindow.Mode }
  target {
    arn      = aws_lambda_function.worker[0].arn
    role_arn = aws_iam_role.scheduler[0].arn
    input    = local.daily.Target.Input
    retry_policy {
      maximum_retry_attempts       = local.daily.Target.RetryPolicy.MaximumRetryAttempts
      maximum_event_age_in_seconds = local.daily.Target.RetryPolicy.MaximumEventAgeInSeconds
    }
  }
  # The protected activation workflow owns the operating state after creation.
  lifecycle { ignore_changes = [state] }
  depends_on = [aws_iam_role_policy.scheduler, aws_lambda_function_event_invoke_config.worker]
}
