# Offline checks on the deploy roles. The provider is mocked, so no account
# is touched and no credentials are needed: `task infra:check`.

mock_provider "aws" {
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::793680745829:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::793680745829:policy/soccer-server-boundary-mock" }
  }
}

run "one_deploy_role_per_deployment_outside_its_own_names" {
  command = plan

  assert {
    condition     = toset(keys(aws_iam_role.deployer)) == toset(["dev", "prod"])
    error_message = "There is one deploy role per deployment in infra/environments/, dev and prod."
  }
  assert {
    condition     = aws_iam_role.deployer["dev"].name == "soccer-deployer-dev" && aws_iam_role.deployer["prod"].name == "soccer-deployer-prod"
    error_message = "The deploy roles are named soccer-deployer-<env>, outside the soccer-<env>-* names they manage, so they can never change themselves."
  }
  assert {
    condition     = aws_iam_policy.server_boundary["dev"].name == "soccer-server-boundary-dev" && aws_iam_policy.server_boundary["prod"].name == "soccer-server-boundary-prod"
    error_message = "The boundaries keep the names infra/environments/<env>.tfvars give as permissions_boundary_arn."
  }
}

run "each_role_trusts_only_its_github_environment" {
  command = plan

  assert {
    condition = (
      jsondecode(aws_iam_role.deployer["dev"].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:CraigDevJohnson@42454849/soccer@1412575682:environment:dev" &&
      jsondecode(aws_iam_role.deployer["prod"].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:CraigDevJohnson@42454849/soccer@1412575682:environment:production"
    )
    error_message = "dev trusts only jobs in the dev environment, prod only jobs in production, the one Craig approves."
  }
  assert {
    condition = alltrue([for role in aws_iam_role.deployer : (
      length(jsondecode(role.assume_role_policy).Statement) == 1 &&
      jsondecode(role.assume_role_policy).Statement[0].Action == "sts:AssumeRoleWithWebIdentity" &&
      jsondecode(role.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com" &&
      keys(jsondecode(role.assume_role_policy).Statement[0].Condition) == ["StringEquals"]
    )])
    error_message = "Each trust is one statement: the exact aud and sub with StringEquals, no wildcards."
  }
}

run "each_role_reaches_only_its_own_deployment" {
  command = plan

  assert {
    condition = alltrue(flatten([for env, policy in aws_iam_role_policy.deployer : [
      for statement in jsondecode(policy.policy).Statement : [
        for resource in statement.Resource :
        resource == "*" || resource == "arn:aws:s3:::craigdevjohnson-tofu-state-793680745829" || strcontains(resource, "soccer-${env}-") || strcontains(resource, "/soccer/${env}/")
      ]
    ]]))
    error_message = "Every resource a deploy role names is its own deployment's (soccer-<env>-*, soccer/<env>/), the state bucket for listing, or a read that AWS evaluates account-wide; metric filters are read only on its own log groups."
  }
  assert {
    condition = alltrue([for env, policy in aws_iam_role_policy.deployer : alltrue([
      for statement in jsondecode(policy.policy).Statement :
      !contains(statement.Resource, "*") || (statement.Resource == ["*"] && contains([["cloudwatch:DescribeAlarms"], ["logs:DescribeLogGroups"]], statement.Action))
    ])])
    error_message = "Only DescribeAlarms and DescribeLogGroups, whose settings hold no secrets, are granted on every resource."
  }
  assert {
    condition = alltrue([for env, policy in aws_iam_role_policy.deployer : alltrue([
      for statement in jsondecode(policy.policy).Statement : alltrue([
        for action in statement.Action : !strcontains(action, "*") || contains(["lambda:Get*", "lambda:List*"], action)
      ])
    ])])
    error_message = "Writes are named one by one; only the deployment's own function is read with a wildcard."
  }
}

run "the_backend_stays_private_whatever_a_commit_says" {
  command = plan

  assert {
    condition = alltrue([for env, policy in aws_iam_role_policy.deployer : anytrue([
      for statement in jsondecode(policy.policy).Statement :
      statement.Sid == "FunctionUrlIamOnly" && statement.Condition.StringEqualsIfExists["lambda:FunctionUrlAuthType"] == "AWS_IAM" && toset(statement.Action) == toset(["lambda:CreateFunctionUrlConfig", "lambda:UpdateFunctionUrlConfig"])
    ])])
    error_message = "A Function URL may only be created with AWS_IAM auth, and an update may never name another auth type."
  }
  assert {
    condition = (
      anytrue([for statement in jsondecode(aws_iam_role_policy.deployer["dev"].policy).Statement :
      statement.Sid == "InvokeGrantsToTheSiteOnly" && statement.Condition.StringEquals["lambda:Principal"] == "arn:aws:iam::793680745829:role/site-dev-server"]) &&
      anytrue([for statement in jsondecode(aws_iam_role_policy.deployer["prod"].policy).Statement :
      statement.Sid == "InvokeGrantsToTheSiteOnly" && statement.Condition.StringEquals["lambda:Principal"] == "arn:aws:iam::793680745829:role/site-prod-server"])
    )
    error_message = "A call to the function may only be granted to the site's role in the same environment."
  }
  assert {
    condition = alltrue([for env, policy in aws_iam_role_policy.deployer : alltrue([
      for statement in jsondecode(policy.policy).Statement :
      statement.Effect == "Deny" || length(setintersection(toset(statement.Action), toset(["lambda:AddPermission", "lambda:CreateFunctionUrlConfig", "lambda:UpdateFunctionUrlConfig", "iam:CreateRole", "iam:PutRolePolicy", "iam:PutRolePermissionsBoundary", "iam:AttachRolePolicy", "cloudwatch:PutMetricAlarm", "cloudwatch:PutCompositeAlarm", "cloudwatch:PutLogAlarm"]))) == 0 || contains(["FunctionUrlIamOnly", "InvokeGrantsToTheSiteOnly", "RoleInsideBoundary", "AlarmsNotifyOnlyAlerts"], statement.Sid)
    ])])
    error_message = "Granting calls, opening the Function URL, giving a role power and choosing what an alarm does happen only in the four conditioned statements."
  }
  assert {
    condition = alltrue([for env, policy in aws_iam_role_policy.deployer : anytrue([
      for statement in jsondecode(policy.policy).Statement :
      statement.Sid == "AlarmsNotifyOnlyAlerts" && statement.Action == ["cloudwatch:PutMetricAlarm"] && keys(statement.Condition) == ["ForAllValues:StringEquals"] && statement.Condition["ForAllValues:StringEquals"]["cloudwatch:AlarmActions"] == ["arn:aws:sns:us-west-2:793680745829:alerts"]
    ])])
    error_message = "An alarm may only notify the alerts topic, so a commit cannot make one stop, reboot or terminate another app's instance."
  }
}

run "the_function_role_changes_only_inside_the_boundary_which_stays_on" {
  command = plan

  # Each boundary gets its own ARN here, so the check below tells them apart.
  override_resource {
    target = aws_iam_policy.server_boundary["dev"]
    values = { arn = "arn:aws:iam::793680745829:policy/soccer-server-boundary-dev" }
  }
  override_resource {
    target = aws_iam_policy.server_boundary["prod"]
    values = { arn = "arn:aws:iam::793680745829:policy/soccer-server-boundary-prod" }
  }

  assert {
    condition = alltrue([for env, policy in aws_iam_role_policy.deployer : anytrue([
      for statement in jsondecode(policy.policy).Statement :
      statement.Sid == "RoleInsideBoundary" && statement.Condition.StringEquals["iam:PermissionsBoundary"] == "arn:aws:iam::793680745829:policy/soccer-server-boundary-${env}"
    ])])
    error_message = "PutRolePolicy and PutRolePermissionsBoundary require this deployment's own boundary, never the other's."
  }
  assert {
    condition = alltrue([for env, policy in aws_iam_role_policy.deployer : anytrue([
      for statement in jsondecode(policy.policy).Statement :
      statement.Effect == "Deny" && contains(statement.Action, "iam:DeleteRolePermissionsBoundary")
    ])])
    error_message = "The boundary can never be taken off a role."
  }
  assert {
    condition = alltrue([for env, policy in aws_iam_role_policy.deployer : anytrue([
      for statement in jsondecode(policy.policy).Statement :
      statement.Sid == "PassRoleToLambdaOnly" && statement.Condition.StringEquals["iam:PassedToService"] == "lambda.amazonaws.com"
    ])])
    error_message = "A role is handed to Lambda and to nothing else."
  }
  assert {
    condition = alltrue([for env, policy in aws_iam_role_policy.deployer : alltrue([
      for statement in jsondecode(policy.policy).Statement :
      statement.Effect == "Deny" || alltrue([for action in statement.Action : !startswith(action, "iam:")]) || statement.Resource == ["arn:aws:iam::793680745829:role/soccer-${env}-server"]
    ])])
    error_message = "The deploy role reaches one role, its deployment's function role soccer-<env>-server, and no other soccer-<env>-* name."
  }
  assert {
    condition = alltrue([for env, policy in aws_iam_role_policy.deployer : alltrue([
      for statement in jsondecode(policy.policy).Statement :
      statement.Effect == "Deny" || length(setintersection(toset(statement.Action), toset(["iam:CreateRole", "iam:UpdateAssumeRolePolicy", "iam:UpdateRole", "iam:DeleteRole"]))) == 0
    ])])
    error_message = "The deploy role never sets a trust policy: it can neither make the function's role, change its trust or session length, nor delete it."
  }
  assert {
    condition = alltrue([for env, policy in aws_iam_role_policy.deployer : length(setsubtract(
      toset(["iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:PutRolePermissionsBoundary", "iam:TagRole", "iam:UntagRole", "iam:UpdateRoleDescription", "iam:PassRole"]),
      toset(flatten([for statement in jsondecode(policy.policy).Statement : statement.Action if statement.Effect == "Allow" && statement.Resource == ["arn:aws:iam::793680745829:role/soccer-${env}-server"]]))
    )) == 0])
    error_message = "The deploy role keeps every call provider 6.68.0 makes to read the function's role and change its policy, boundary, tags and description, and to hand it to Lambda."
  }
  assert {
    condition = alltrue([for env, boundary in aws_iam_policy.server_boundary : (
      length(jsondecode(boundary.policy).Statement) == 1 &&
      toset(jsondecode(boundary.policy).Statement[0].Action) == toset(["logs:CreateLogStream", "logs:PutLogEvents"]) &&
      jsondecode(boundary.policy).Statement[0].Resource == ["arn:aws:logs:us-west-2:793680745829:log-group:/aws/lambda/soccer-${env}-*:*"]
    )])
    error_message = "A soccer function's role can at most write its own deployment's logs (aws-design.md G7)."
  }
}

run "state_is_only_the_deployments_own" {
  command = plan

  assert {
    condition = alltrue([for env, policy in aws_iam_role_policy.deployer : anytrue([
      for statement in jsondecode(policy.policy).Statement :
      statement.Sid == "StateReadWrite" && statement.Resource == ["arn:aws:s3:::craigdevjohnson-tofu-state-793680745829/soccer/${env}/*"]
    ])])
    error_message = "A deploy role reads and writes only its own state key and lock file, never the bootstrap's or the site's."
  }
  assert {
    condition = alltrue([for env, policy in aws_iam_role_policy.deployer : anytrue([
      for statement in jsondecode(policy.policy).Statement :
      statement.Sid == "StateList" && statement.Action == ["s3:ListBucket"] && keys(statement.Condition) == ["StringLike"] && statement.Condition.StringLike["s3:prefix"] == ["soccer/${env}/*"]
    ])])
    error_message = "A deploy role lists only keys under its own soccer/<env>/ prefix, where its tfbackend's workspace_key_prefix keeps the workspace list too."
  }
  assert {
    condition = alltrue([for env, policy in aws_iam_role_policy.deployer : alltrue([
      for statement in jsondecode(policy.policy).Statement :
      statement.Effect == "Deny" ||
      (!contains(statement.Resource, "arn:aws:s3:::craigdevjohnson-tofu-state-793680745829") && alltrue([for action in statement.Action : !startswith(action, "s3:")])) ||
      (statement.Sid == "StateList" && statement.Action == ["s3:ListBucket"] && statement.Resource == ["arn:aws:s3:::craigdevjohnson-tofu-state-793680745829"]) ||
      (statement.Sid == "StateReadWrite" && toset(statement.Action) == toset(["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]))
    ])])
    error_message = "S3 is reached only through StateList (the bucket, listed under soccer/<env>/ only) and StateReadWrite (its own keys); no other statement names the state bucket or an S3 action."
  }
}

run "a_wildcard_subject_is_refused" {
  command = plan

  variables {
    github_subject_prefix = "repo:CraigDevJohnson/*"
  }
  expect_failures = [var.github_subject_prefix]
}
