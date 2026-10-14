output "deployer_role_arns" {
  description = "Per deployment, the role its Deploy job assumes: the value of AWS_DEV_DEPLOYER_ROLE_ARN in the GitHub environment dev and AWS_PROD_DEPLOYER_ROLE_ARN in production."
  value       = { for env, role in aws_iam_role.deployer : env => role.arn }
}

output "server_boundary_arns" {
  description = "Per deployment, the permissions boundary its function's role carries: permissions_boundary_arn in infra/environments/<env>.tfvars."
  value       = { for env, policy in aws_iam_policy.server_boundary : env => policy.arn }
}
