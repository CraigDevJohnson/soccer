mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/mock" }
  }
}

variables {
  account_id       = "111122223333"
  site_environment = "prod"
}

run "production_recovery_starts_with_storage_before_any_image" {
  command = plan
  assert {
    condition = (
      length(aws_lambda_function.worker) == 0 && aws_s3_bucket.recovery[0].bucket == "soccer-archive-prod-recovery-111122223333" &&
      !aws_s3_bucket.recovery[0].force_destroy && one(aws_s3_bucket_versioning.recovery[0].versioning_configuration).status == "Enabled" &&
      one(aws_s3_bucket_ownership_controls.recovery[0].rule).object_ownership == "BucketOwnerEnforced" &&
      one(one(aws_s3_bucket_server_side_encryption_configuration.recovery[0].rule).apply_server_side_encryption_by_default).sse_algorithm == "AES256" &&
      aws_s3_bucket_public_access_block.recovery[0].block_public_acls && aws_s3_bucket_public_access_block.recovery[0].block_public_policy &&
      aws_s3_bucket_public_access_block.recovery[0].ignore_public_acls && aws_s3_bucket_public_access_block.recovery[0].restrict_public_buckets
    )
    error_message = "The production bootstrap must provision separate private, versioned, encrypted recovery storage."
  }
  assert {
    condition = (
      length(aws_s3_bucket_replication_configuration.recovery[0].rule) == 3 &&
      toset([for r in aws_s3_bucket_replication_configuration.recovery[0].rule : one(r.filter).prefix]) == toset(["generations/", "journal/", "control/"]) &&
      alltrue([for r in aws_s3_bucket_replication_configuration.recovery[0].rule :
        r.status == "Enabled" && one(r.delete_marker_replication).status == "Disabled" &&
        one(r.destination).bucket == "arn:aws:s3:::soccer-archive-prod-recovery-111122223333"
      ])
    )
    error_message = "Every new generation, control record version and journal record must replicate without source deletions."
  }
  assert {
    condition = (
      jsondecode(aws_s3_bucket_policy.recovery[0].policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false" &&
      jsondecode(aws_s3_bucket_policy.recovery[0].policy).Statement[1].Effect == "Deny" &&
      toset(jsondecode(aws_s3_bucket_policy.recovery[0].policy).Statement[1].Action) == toset(["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:ReplicateDelete"]) &&
      jsondecode(aws_iam_role.replication[0].assume_role_policy).Statement[0].Principal.Service == "s3.amazonaws.com" &&
      jsondecode(aws_iam_role_policy.replication[0].policy).Statement[2].Action == ["s3:ReplicateObject"] &&
      jsondecode(aws_iam_role_policy.replication[0].policy).Statement[2].Resource == [for p in ["generations", "journal", "control"] : "arn:aws:s3:::soccer-archive-prod-recovery-111122223333/${p}/*"]
    )
    error_message = "S3 may replicate only the protected prefixes. Direct writes and deletes must be denied."
  }
  assert {
    condition = (
      jsondecode(aws_iam_role.github["recovery"].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:CraigDevJohnson@42454849/soccer@1412575682:environment:archive-recovery-prod" &&
      toset(jsondecode(aws_iam_role_policy.recovery_verifier[0].policy).Statement[0].Action) == toset(["s3:GetObject", "s3:GetObjectVersion"]) &&
      length(jsondecode(aws_iam_role_policy.recovery_verifier[0].policy).Statement[0].Resource) == 6 &&
      !strcontains(aws_iam_role_policy.import.policy, "prod-recovery")
    )
    error_message = "Production recovery verification uses separate read-only credentials."
  }
}

run "dev_has_no_recovery_bucket_or_replication_permissions" {
  command = plan
  variables { site_environment = "dev" }
  assert {
    condition = (
      length(aws_s3_bucket.recovery) == 0 && length(aws_iam_role.replication) == 0 &&
      length(aws_s3_bucket_replication_configuration.recovery) == 0 && !contains(keys(aws_iam_role.github), "recovery")
    )
    error_message = "The additional retained copy is for production only."
  }
}
