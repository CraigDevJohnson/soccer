# Offline checks on the root. The provider is mocked, so no account is
# touched and no credentials are needed: `task infra:check`.

# The provider validates some computed values at plan time (ARNs), so those
# get realistic shapes here.
mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/soccer-dev-server" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-west-2:111122223333:log-group:/aws/lambda/soccer-dev-server" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:us-west-2:111122223333:function:soccer-dev-server" }
  }
  mock_resource "aws_lambda_function_url" {
    defaults = { function_url = "https://mock.lambda-url.us-west-2.on.aws/" }
  }
}

variables {
  account_id       = "111122223333"
  site_environment = "dev"
  site_role_arn    = "arn:aws:iam::111122223333:role/site-dev-server"
  package_path     = "tests/fixtures/soccer.placeholder"
  link_base_url    = "https://site.example.com/soccer/link/"
  alert_topic_arn  = "arn:aws:sns:us-west-2:111122223333:alerts"
}

run "names_carry_the_site_environment" {
  command = plan

  assert {
    condition     = aws_lambda_function.server.function_name == "soccer-dev-server"
    error_message = "The function must be named <resource_prefix>-<site_environment>-server, so the site's boundary (*-dev-*) lets the site call it."
  }
  assert {
    condition     = aws_iam_role.server.name == "soccer-dev-server" && aws_cloudwatch_log_group.server.name == "/aws/lambda/soccer-dev-server"
    error_message = "The role and the log group take the function's name."
  }
  assert {
    condition     = aws_lambda_function.server.runtime == "provided.al2023" && aws_lambda_function.server.architectures == tolist(["arm64"]) && aws_lambda_function.server.handler == "bootstrap"
    error_message = "The zip from `task lambda` is one static arm64 binary named bootstrap for provided.al2023."
  }
  assert {
    condition     = aws_cloudwatch_log_group.server.retention_in_days == 30
    error_message = "Logs are kept 30 days by default (aws-design.md G8)."
  }
}

run "only_the_site_role_may_call_the_address" {
  command = plan

  assert {
    condition     = aws_lambda_function_url.server.authorization_type == "AWS_IAM"
    error_message = "The Function URL must require a signed request."
  }
  assert {
    condition     = aws_lambda_permission.site_url.principal == var.site_role_arn && aws_lambda_permission.site_url.action == "lambda:InvokeFunctionUrl" && aws_lambda_permission.site_url.function_url_auth_type == "AWS_IAM"
    error_message = "lambda:InvokeFunctionUrl goes to the site's role alone."
  }
  assert {
    condition     = aws_lambda_permission.site_invoke.principal == var.site_role_arn && aws_lambda_permission.site_invoke.action == "lambda:InvokeFunction" && aws_lambda_permission.site_invoke.invoked_via_function_url == true
    error_message = "lambda:InvokeFunction goes to the site's role alone, and only through the URL."
  }
}

run "the_role_may_only_write_its_own_logs" {
  command = plan

  assert {
    condition = (
      length(jsondecode(aws_iam_role_policy.server.policy).Statement) == 1 &&
      toset(jsondecode(aws_iam_role_policy.server.policy).Statement[0].Action) == toset(["logs:CreateLogStream", "logs:PutLogEvents"]) &&
      jsondecode(aws_iam_role_policy.server.policy).Statement[0].Resource == ["arn:aws:logs:us-west-2:111122223333:log-group:/aws/lambda/soccer-dev-server:*"]
    )
    error_message = "The function's policy must allow exactly its own log streams and nothing else (aws-design.md G7)."
  }
  assert {
    condition     = jsondecode(aws_iam_role.server.assume_role_policy).Statement[0].Principal.Service == "lambda.amazonaws.com"
    error_message = "Only Lambda may assume the role."
  }
  assert {
    condition     = aws_iam_role.server.permissions_boundary == null
    error_message = "No boundary unless one is given."
  }
}

run "the_code_gets_its_settings" {
  command = plan

  assert {
    condition     = aws_lambda_function.server.environment[0].variables == tomap({ LINK_BASE_URL = "https://site.example.com/soccer/link/" })
    error_message = "LINK_BASE_URL is set, and the league settings are left to the code's defaults when unset."
  }
  assert {
    condition     = aws_lambda_function.server.timeout == 10 && aws_lambda_function.server.memory_size == 256
    error_message = "A request answers within the router's 8-second deadline; 10 seconds covers it like the site's own function."
  }
}

run "league_settings_are_passed_when_set" {
  command = plan

  variables {
    league_base_url    = "https://league.example.com"
    league_facility_id = "7"
  }

  assert {
    condition = aws_lambda_function.server.environment[0].variables == tomap({
      LINK_BASE_URL      = "https://site.example.com/soccer/link/"
      LEAGUE_BASE_URL    = "https://league.example.com"
      LEAGUE_FACILITY_ID = "7"
    })
    error_message = "The league settings reach the code when given."
  }
}

run "alarms_notify_the_alerts_topic" {
  command = plan

  assert {
    condition     = aws_cloudwatch_log_metric_filter.league_unavailable.pattern == "{ $.msg = \"league_unavailable\" }" && aws_cloudwatch_log_metric_filter.league_unavailable.log_group_name == "/aws/lambda/soccer-dev-server"
    error_message = "The filter counts the code's league_unavailable log lines (slog JSON, key msg)."
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.league_unavailable.namespace == "Soccer/Backend/dev" && aws_cloudwatch_metric_alarm.league_unavailable.metric_name == "LeagueUnavailable"
    error_message = "The alarm watches the filter's metric, one namespace per site environment."
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.league_unavailable.alarm_actions == toset([var.alert_topic_arn]) && aws_cloudwatch_metric_alarm.league_unavailable.ok_actions == toset([var.alert_topic_arn])
    error_message = "The league alarm notifies the account's alerts topic, and clears there too."
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.failing.namespace == "AWS/Lambda" && aws_cloudwatch_metric_alarm.failing.metric_name == "Errors" && aws_cloudwatch_metric_alarm.failing.dimensions["FunctionName"] == "soccer-dev-server" && aws_cloudwatch_metric_alarm.failing.alarm_actions == toset([var.alert_topic_arn])
    error_message = "The failing alarm watches this function's Lambda errors and notifies the alerts topic."
  }
}

run "link_base_url_must_be_the_site_link_path" {
  command = plan

  variables {
    link_base_url = "https://site.example.com/"
  }

  expect_failures = [var.link_base_url]
}

run "the_site_role_must_be_in_the_same_account" {
  command = plan

  variables {
    site_role_arn = "arn:aws:iam::444455556666:role/site-dev-server"
  }

  expect_failures = [var.site_role_arn]
}

run "the_alerts_topic_must_be_in_the_same_account_and_region" {
  command = plan

  variables {
    alert_topic_arn = "arn:aws:sns:us-east-1:111122223333:alerts"
  }

  expect_failures = [var.alert_topic_arn]
}

run "extra_tags_may_not_replace_the_fixed_ones" {
  command = plan

  variables {
    tags = { project = "something-else" }
  }

  expect_failures = [var.tags]
}

run "the_role_carries_the_boundary_it_is_given" {
  command = plan

  variables {
    permissions_boundary_arn = "arn:aws:iam::111122223333:policy/soccer-server-boundary-dev"
  }

  assert {
    condition     = aws_iam_role.server.permissions_boundary == "arn:aws:iam::111122223333:policy/soccer-server-boundary-dev"
    error_message = "The function's role carries the boundary infra/bootstrap made, which the deploy role requires."
  }
}

run "the_boundary_must_be_in_the_same_account" {
  command = plan

  variables {
    permissions_boundary_arn = "arn:aws:iam::444455556666:policy/soccer-server-boundary-dev"
  }

  expect_failures = [var.permissions_boundary_arn]
}

run "the_site_is_reported_for_the_smoke_test" {
  command = plan

  assert {
    condition     = output.link_base_url == "https://site.example.com/soccer/link/"
    error_message = "task infra:smoke reads where the site serves Session links from the link_base_url output."
  }
}
