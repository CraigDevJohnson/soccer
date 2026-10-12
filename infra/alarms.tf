# Two alarms, both to the account's alerts topic, which emails Craig.

# The code logs one `league_unavailable` line each time the league's
# schedule service fails a request (README, "Errors"). This counts those
# lines; the alarm fires on the first in any five minutes and clears after
# five minutes without one.
resource "aws_cloudwatch_log_metric_filter" "league_unavailable" {
  name           = "${local.name}-league-unavailable"
  log_group_name = aws_cloudwatch_log_group.server.name
  pattern        = "{ $.msg = \"league_unavailable\" }"

  metric_transformation {
    name          = "LeagueUnavailable"
    namespace     = "Soccer/Backend/${var.site_environment}"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "league_unavailable" {
  alarm_name          = "${local.name}-league-unavailable"
  alarm_description   = "The league's schedule service failed a Let's Plan Soccer request, so downloads and Session links are answering 503. See docs/operations/deploy.md."
  namespace           = aws_cloudwatch_log_metric_filter.league_unavailable.metric_transformation[0].namespace
  metric_name         = aws_cloudwatch_log_metric_filter.league_unavailable.metric_transformation[0].name
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alert_topic_arn]
  ok_actions          = [var.alert_topic_arn]
}

# A crash or a run that hit Lambda's timeout; the code answers every other
# failure itself, as a 5xx the site relays.
resource "aws_cloudwatch_metric_alarm" "failing" {
  alarm_name          = "${local.name}-failing"
  alarm_description   = "The Let's Plan Soccer backend crashed or timed out. See docs/operations/deploy.md."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.server.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alert_topic_arn]
  ok_actions          = [var.alert_topic_arn]
}
