output "function_name" {
  description = "The function's name, <resource_prefix>-<site_environment>-server."
  value       = aws_lambda_function.server.function_name
}

output "function_arn" {
  description = "What the site's role is granted lambda:InvokeFunctionUrl and lambda:InvokeFunction on (the-lobby's site module)."
  value       = aws_lambda_function.server.arn
}

output "function_url" {
  description = "The site's SOCCER_BACKEND_URL. The site drops the trailing slash itself."
  value       = aws_lambda_function_url.server.function_url
}

output "log_group_name" {
  description = "Where the function's JSON log lines go."
  value       = aws_cloudwatch_log_group.server.name
}
