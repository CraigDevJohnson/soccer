mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/mock" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-west-2:111122223333:log-group:/aws/lambda/soccer-archive-dev-worker" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:us-west-2:111122223333:function:soccer-archive-dev-worker" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = {
      arn            = "arn:aws:ecr:us-west-2:111122223333:repository/soccer-archive-dev-worker"
      repository_url = "111122223333.dkr.ecr.us-west-2.amazonaws.com/soccer-archive-dev-worker"
    }
  }
  mock_resource "aws_scheduler_schedule_group" {
    defaults = { arn = "arn:aws:scheduler:us-west-2:111122223333:schedule-group/soccer-archive-dev" }
  }
}

variables {
  account_id       = "111122223333"
  site_environment = "dev"
}

run "empty_environment_is_private_and_idle" {
  command = plan
  assert {
    condition = (
      aws_s3_bucket.archive.bucket == "soccer-archive-dev-111122223333" && !aws_s3_bucket.archive.force_destroy &&
      aws_s3_bucket_public_access_block.archive.block_public_acls && aws_s3_bucket_public_access_block.archive.block_public_policy &&
      aws_s3_bucket_public_access_block.archive.ignore_public_acls && aws_s3_bucket_public_access_block.archive.restrict_public_buckets &&
      one(aws_s3_bucket_versioning.archive.versioning_configuration).status == "Enabled" &&
      one(aws_s3_bucket_ownership_controls.archive.rule).object_ownership == "BucketOwnerEnforced" &&
      one(one(aws_s3_bucket_server_side_encryption_configuration.archive.rule).apply_server_side_encryption_by_default).sse_algorithm == "AES256"
    )
    error_message = "Private, versioned, bucket-owner-enforced SSE-S3 storage is required."
  }
  assert {
    condition = (
      length(aws_lambda_function.worker) == 0 && length(aws_scheduler_schedule.daily) == 0 && length(aws_iam_role.stage) == 0 &&
      aws_ecr_repository.worker.image_tag_mutability == "IMMUTABLE" && !aws_ecr_repository.worker.force_delete
    )
    error_message = "A first apply must allow image publication without starting a collector or granting staging access."
  }
  assert {
    condition = (
      jsondecode(aws_s3_bucket_policy.archive.policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false" &&
      jsondecode(aws_s3_bucket_policy.archive.policy).Statement[1].Resource == [for prefix in ["staging", "generations", "journal"] : "arn:aws:s3:::soccer-archive-dev-111122223333/${prefix}/*"] &&
      jsondecode(aws_s3_bucket_policy.archive.policy).Statement[1].Condition.Null["s3:if-none-match"] == "true" &&
      toset(keys(jsondecode(aws_s3_bucket_policy.archive.policy).Statement[2].Condition.Null)) == toset(["s3:if-match", "s3:if-none-match"]) &&
      alltrue([for rule in aws_s3_bucket_lifecycle_configuration.archive.rule : length(rule.expiration) == 0])
    )
    error_message = "Bucket policy must require TLS and conditional writes. Lifecycle must keep every current archive object."
  }
}

run "protected_roles_separate_import_image_and_invocation" {
  command = plan
  assert {
    condition = alltrue([for purpose, role in aws_iam_role.github :
      jsondecode(role.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com" &&
      jsondecode(role.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:CraigDevJohnson@42454849/soccer@1412575682:environment:archive-${purpose}-dev"
    ])
    error_message = "Each role must trust its own protected, repository-specific GitHub environment."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.run.policy).Statement == [{
      Effect = "Allow", Action = "lambda:InvokeFunction", Resource = "arn:aws:lambda:us-west-2:111122223333:function:soccer-archive-dev-worker"
    }]
    error_message = "A manual run can invoke only this environment's worker."
  }
  assert {
    condition = (
      length(jsondecode(aws_iam_role_policy.import.policy).Statement) == 2 &&
      jsondecode(aws_iam_role_policy.import.policy).Statement[1].Action == "s3:PutObject" &&
      jsondecode(aws_iam_role_policy.import.policy).Statement[1].Condition.StringEquals["s3:if-none-match"] == "*" &&
      jsondecode(aws_iam_role_policy.import.policy).Statement[1].Resource == ["arn:aws:s3:::soccer-archive-dev-111122223333/generations/*", "arn:aws:s3:::soccer-archive-dev-111122223333/control/active.json"]
    )
    error_message = "The baseline importer can seed the pointer exactly once."
  }
  assert {
    condition = (
      length(jsondecode(aws_iam_role_policy.image.policy).Statement) == 2 &&
      jsondecode(aws_iam_role_policy.image.policy).Statement[0].Action == "ecr:GetAuthorizationToken" &&
      jsondecode(aws_iam_role_policy.image.policy).Statement[1].Resource == "arn:aws:ecr:us-west-2:111122223333:repository/soccer-archive-dev-worker" &&
      alltrue([for a in jsondecode(aws_iam_role_policy.image.policy).Statement[1].Action : startswith(a, "ecr:")])
    )
    error_message = "Image publishing must be scoped to one repository."
  }
}

run "worker_and_schedule_have_separate_minimal_roles" {
  command = plan
  variables {
    image_digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
  assert {
    condition = (
      aws_lambda_function.worker[0].memory_size == 2048 && aws_lambda_function.worker[0].timeout == 900 &&
      aws_lambda_function.worker[0].reserved_concurrent_executions == 1 && one(aws_lambda_function.worker[0].ephemeral_storage).size >= 2048 &&
      one(aws_lambda_function.worker[0].architectures) == "arm64" && endswith(aws_lambda_function.worker[0].image_uri, "@${var.image_digest}") &&
      one(aws_lambda_function.worker[0].environment).variables["ARCHIVE_BUCKET"] == "soccer-archive-dev-111122223333"
    )
    error_message = "The worker must run the approved image digest, process one bounded invocation, and use its environment's archive."
  }
  assert {
    condition = (
      aws_cloudwatch_log_group.worker[0].retention_in_days == 30 && aws_cloudwatch_metric_alarm.errors[0].metric_name == "Errors" &&
      one(aws_cloudwatch_metric_alarm.errors[0].alarm_actions) == "arn:aws:sns:us-west-2:111122223333:alerts" &&
      aws_scheduler_schedule.daily[0].state == "DISABLED" && aws_scheduler_schedule.daily[0].schedule_expression_timezone == "America/Boise" &&
      strcontains(one(aws_scheduler_schedule.daily[0].target).input, "<aws.scheduler.scheduled-time>") &&
      length(jsondecode(aws_iam_role_policy.activation.policy).Statement) == 3 &&
      jsondecode(aws_iam_role_policy.worker[0].policy).Statement[2].Action == "s3:GetObject" &&
      jsondecode(aws_iam_role_policy.worker[0].policy).Statement[2].Resource == "arn:aws:s3:::soccer-archive-dev-111122223333/settings/collection.json" &&
      aws_lambda_function_event_invoke_config.worker[0].maximum_retry_attempts == 0
    )
    error_message = "Bound retries and report function errors. Recurring collection must be opt-in."
  }
  assert {
    condition = (
      length(jsondecode(aws_iam_role_policy.scheduler[0].policy).Statement) == 1 &&
      jsondecode(aws_iam_role_policy.scheduler[0].policy).Statement[0].Resource == "arn:aws:lambda:us-west-2:111122223333:function:soccer-archive-dev-worker" &&
      jsondecode(aws_iam_role.scheduler[0].assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceArn"] == "arn:aws:scheduler:us-west-2:111122223333:schedule-group/soccer-archive-dev" &&
      toset(jsondecode(aws_iam_role_policy.worker[0].policy).Statement[1].Action) == toset(["s3:PutObject", "s3:GetObject"]) &&
      jsondecode(aws_iam_role_policy.worker[0].policy).Statement[1].Resource == [for prefix in ["generations", "control", "journal"] : "arn:aws:s3:::soccer-archive-dev-111122223333/${prefix}/*"]
    )
    error_message = "The scheduler can invoke only its worker. The worker must have no staging or delete access."
  }
}

run "staging_is_one_generation_from_one_identity" {
  command = plan
  variables {
    staging_access = {
      principal_arn = "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/us-west-2/AWSReservedSSO_WorkloadsAdmin_1234567890abcdef"
      generation    = "1111111111111111111111111111111111111111111111111111111111111111"
    }
  }
  assert {
    condition = (
      jsondecode(aws_iam_role.stage[0].assume_role_policy).Statement[0].Principal.AWS == var.staging_access.principal_arn &&
      jsondecode(aws_iam_role_policy.stage[0].policy).Statement[0].Resource == [for file in ["archive.tar.gz", "manifest.json"] : "arn:aws:s3:::soccer-archive-dev-111122223333/staging/${var.staging_access.generation}/${file}"]
    )
    error_message = "The operator role can stage only the named baseline."
  }
}

run "reject_unpinned_image_tag" {
  command = plan
  variables { image_digest = "latest" }
  expect_failures = [var.image_digest]
}
run "reject_broad_staging_principal" {
  command = plan
  variables {
    staging_access = {
      principal_arn = "arn:aws:iam::111122223333:root"
      generation    = "1111111111111111111111111111111111111111111111111111111111111111"
    }
  }
  expect_failures = [var.staging_access]
}

run "production_promotion_keeps_dev_read_only" {
  command = plan
  variables {
    site_environment = "prod"
    image_digest     = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
  assert {
    condition = (
      aws_scheduler_schedule.daily[0].state == "DISABLED" &&
      jsondecode(aws_iam_role_policy.activation.policy).Statement[3].Resource == "arn:aws:scheduler:us-west-2:111122223333:schedule/soccer-archive-prod/refresh" &&
      jsondecode(aws_iam_role_policy.activation.policy).Statement[4].Condition.StringEquals["iam:PassedToService"] == "scheduler.amazonaws.com" &&
      jsondecode(aws_iam_role_policy.activation.policy).Statement[4].Resource == "arn:aws:iam::111122223333:role/soccer-archive-prod-scheduler" &&
      jsondecode(aws_iam_role_policy.release.policy).Statement[0].Resource == "arn:aws:lambda:us-west-2:111122223333:function:soccer-archive-prod-worker" &&
      jsondecode(aws_iam_role_policy.release.policy).Statement[2].Action == ["lambda:GetFunction"] &&
      jsondecode(aws_iam_role_policy.release.policy).Statement[2].Resource == "arn:aws:lambda:us-west-2:111122223333:function:soccer-archive-dev-worker" &&
      toset(jsondecode(aws_iam_role_policy.image.policy).Statement[2].Action) == toset(["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability", "ecr:DescribeImages"]) &&
      jsondecode(aws_iam_role_policy.image.policy).Statement[2].Resource == "arn:aws:ecr:us-west-2:111122223333:repository/soccer-archive-dev-worker"
    )
    error_message = "Production can read the tested dev image and release to its own worker only."
  }
}

run "dev_activation_changes_only_the_collection_mode" {
  command = plan
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.activation.policy).Statement[0].Resource == "arn:aws:s3:::soccer-archive-dev-111122223333/settings/collection.json" &&
      jsondecode(aws_iam_role_policy.activation.policy).Statement[1].Action == ["s3:GetObject"] &&
      jsondecode(aws_iam_role_policy.activation.policy).Statement[1].Resource == [for key in ["settings/collection.json", "control/worker.json"] : "arn:aws:s3:::soccer-archive-prod-111122223333/${key}"] &&
      jsondecode(aws_iam_role_policy.activation.policy).Statement[2].Condition.StringEquals["s3:prefix"] == ["settings/collection.json", "control/worker.json"] &&
      length(jsondecode(aws_iam_role_policy.activation.policy).Statement) == 3
    )
    error_message = "Development activation must not edit a schedule, the worker, or any archive data."
  }
  assert {
    condition = (
      contains(jsondecode(aws_s3_bucket_policy.archive.policy).Statement[2].Resource, "arn:aws:s3:::soccer-archive-dev-111122223333/settings/collection.json") &&
      jsondecode(aws_iam_role.github["activation"].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:CraigDevJohnson@42454849/soccer@1412575682:environment:archive-activation-dev"
    )
    error_message = "Only the protected activation workflow should operate the switch, with conditional writes."
  }
}

run "attention_and_delivery_notify_existing_alerts" {
  command = plan
  variables {
    site_environment = "prod"
    image_digest     = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
  assert {
    condition = (
      aws_cloudwatch_log_metric_filter.attention[0].log_group_name == "/aws/lambda/soccer-archive-prod-worker" &&
      aws_cloudwatch_log_metric_filter.attention[0].pattern == "{ $.event = \"archive_attention\" }" &&
      aws_cloudwatch_metric_alarm.attention[0].metric_name == "CollectionNeedsAttention" &&
      aws_cloudwatch_metric_alarm.attention[0].namespace == "Soccer/Archive/prod" &&
      aws_cloudwatch_metric_alarm.attention[0].alarm_actions == toset(["arn:aws:sns:us-west-2:111122223333:alerts"]) &&
      aws_cloudwatch_metric_alarm.dropped["scheduler"].dimensions == tomap({ ScheduleGroup = "soccer-archive-prod" }) &&
      aws_cloudwatch_metric_alarm.dropped["scheduler"].metric_name == "InvocationDroppedCount" &&
      aws_cloudwatch_metric_alarm.dropped["lambda"].dimensions == tomap({ FunctionName = "soccer-archive-prod-worker" }) &&
      aws_cloudwatch_metric_alarm.dropped["lambda"].metric_name == "AsyncEventsDropped" &&
      alltrue([for alarm in aws_cloudwatch_metric_alarm.dropped : alarm.alarm_actions == toset(["arn:aws:sns:us-west-2:111122223333:alerts"])])
    )
    error_message = "Page for a job requiring operator action and for expired events on either side of the asynchronous Lambda invocation."
  }
}
