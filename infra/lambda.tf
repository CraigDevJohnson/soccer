# The backend: one Go function behind a Function URL that only the site's
# role may call with signed requests (the-lobby ADR 0006).

locals {
  # The site may call only backends named *-<its environment>-* (the-lobby's
  # site boundary), so the name carries the site's environment.
  name           = "${var.resource_prefix}-${var.site_environment}-server"
  log_group_name = "/aws/lambda/${local.name}"

  # The settings the code reads (README); the optional ones are left out
  # rather than set empty, so the code's defaults apply.
  environment = { for key, value in {
    LINK_BASE_URL      = var.link_base_url
    LEAGUE_BASE_URL    = var.league_base_url
    LEAGUE_FACILITY_ID = var.league_facility_id
  } : key => value if value != null }
}

resource "aws_cloudwatch_log_group" "server" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "server" {
  name = local.name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "LambdaAssumes"
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
  permissions_boundary = var.permissions_boundary_arn
}

# Least privilege (aws-design.md G7): the function writes its own log group
# and touches nothing else in AWS. The league is reached over the internet.
resource "aws_iam_role_policy" "server" {
  name = "logs"
  role = aws_iam_role.server.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "OwnLogsOnly"
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = ["${aws_cloudwatch_log_group.server.arn}:*"]
    }]
  })
}

resource "aws_lambda_function" "server" {
  function_name = local.name
  description   = "The Schedule Downloader's backend for The Lobby (${var.site_environment})."
  role          = aws_iam_role.server.arn

  filename         = var.package_path
  source_code_hash = filebase64sha256(var.package_path)
  handler          = "bootstrap"
  runtime          = "provided.al2023"
  architectures    = ["arm64"]

  memory_size = 256
  # Every request answers within 8 seconds (the router's deadline), and the
  # site gives up at 10, so a longer run would only cost money.
  timeout = 10

  environment {
    variables = local.environment
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.server.name
  }

  depends_on = [aws_iam_role_policy.server]
}

resource "aws_lambda_function_url" "server" {
  function_name      = aws_lambda_function.server.function_name
  authorization_type = "AWS_IAM"
}

# The site's role is the only principal this function grants a call to;
# the second statement is the lambda:InvokeFunction grant AWS also requires
# for a Function URL, limited to calls that come through the URL. AWS lets
# a principal in the same account call the URL on its own identity policy
# too, so the account's workload roles carrying no lambda:InvokeFunctionUrl
# (aws-design.md G7) is what keeps the site the only caller; the admin
# profile can call it, which docs/operations/deploy.md uses for its check.
resource "aws_lambda_permission" "site_url" {
  statement_id           = "AllowSiteThroughFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.server.function_name
  principal              = var.site_role_arn
  function_url_auth_type = "AWS_IAM"
}

resource "aws_lambda_permission" "site_invoke" {
  statement_id             = "AllowSiteInvokeThroughFunctionUrl"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.server.function_name
  principal                = var.site_role_arn
  invoked_via_function_url = true
}
