resource "aws_cloudwatch_log_metric_filter" "attention" {
  count          = local.worker_enabled ? 1 : 0
  name           = "${local.worker_name}-attention"
  log_group_name = aws_cloudwatch_log_group.worker[0].name
  pattern        = "{ $.event = \"archive_attention\" }"
  metric_transformation {
    name      = "CollectionNeedsAttention"
    namespace = "Soccer/Archive/${var.site_environment}"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "attention" {
  count               = local.worker_enabled ? 1 : 0
  alarm_name          = "${local.worker_name}-attention"
  alarm_description   = "A collection job paused, was blocked, or lost an attempt. Inspect job status before resuming."
  namespace           = aws_cloudwatch_log_metric_filter.attention[0].metric_transformation[0].namespace
  metric_name         = aws_cloudwatch_log_metric_filter.attention[0].metric_transformation[0].name
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alerts]
}

resource "aws_cloudwatch_metric_alarm" "dropped" {
  for_each = local.worker_enabled && var.site_environment == "prod" ? {
    scheduler = { namespace = "AWS/Scheduler", metric = "InvocationDroppedCount", dimensions = { ScheduleGroup = local.prefix } }
    lambda    = { namespace = "AWS/Lambda", metric = "AsyncEventsDropped", dimensions = { FunctionName = local.worker_name } }
  } : {}
  alarm_name          = "${local.worker_name}-${each.key}-dropped"
  alarm_description   = "${each.value.namespace} dropped an archive event. Inspect job status before admitting a replacement."
  namespace           = each.value.namespace
  metric_name         = each.value.metric
  dimensions          = each.value.dimensions
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alerts]
}
