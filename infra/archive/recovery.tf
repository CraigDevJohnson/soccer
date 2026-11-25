locals {
  keep_recovery     = var.site_environment == "prod"
  recovery_name     = "${local.prefix}-recovery-${var.account_id}"
  recovery_arn      = "arn:aws:s3:::${local.recovery_name}"
  recovery_prefixes = ["generations", "journal", "control"]
}

resource "aws_s3_bucket" "recovery" {
  count         = local.keep_recovery ? 1 : 0
  bucket        = local.recovery_name
  force_destroy = false
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_public_access_block" "recovery" {
  count                   = local.keep_recovery ? 1 : 0
  bucket                  = aws_s3_bucket.recovery[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "recovery" {
  count  = local.keep_recovery ? 1 : 0
  bucket = aws_s3_bucket.recovery[0].id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_versioning" "recovery" {
  count  = local.keep_recovery ? 1 : 0
  bucket = aws_s3_bucket.recovery[0].id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "recovery" {
  count  = local.keep_recovery ? 1 : 0
  bucket = aws_s3_bucket.recovery[0].id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_policy" "recovery" {
  count  = local.keep_recovery ? 1 : 0
  bucket = aws_s3_bucket.recovery[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "RequireTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [local.recovery_arn, "${local.recovery_arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false", "aws:PrincipalIsAWSService" = "false" } }
      },
      {
        Sid       = "PreserveReplicatedVersions"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:ReplicateDelete"]
        Resource  = "${local.recovery_arn}/*"
      }
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.recovery]
}

resource "aws_iam_role" "replication" {
  count = local.keep_recovery ? 1 : 0
  name  = "${local.prefix}-replication"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "s3.amazonaws.com" }
      Condition = {
        StringEqualsIfExists = { "aws:SourceAccount" = var.account_id }
        ArnEqualsIfExists    = { "aws:SourceArn" = local.bucket_arn }
      }
    }]
  })
}

resource "aws_iam_role_policy" "replication" {
  count = local.keep_recovery ? 1 : 0
  name  = "replicate-archive-versions"
  role  = aws_iam_role.replication[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetReplicationConfiguration", "s3:ListBucket"]
        Resource = local.bucket_arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObjectVersionForReplication", "s3:GetObjectVersionAcl", "s3:GetObjectVersionTagging"]
        Resource = [for p in local.recovery_prefixes : "${local.bucket_arn}/${p}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ReplicateObject"]
        Resource = [for p in local.recovery_prefixes : "${local.recovery_arn}/${p}/*"]
      }
    ]
  })
}

resource "aws_s3_bucket_replication_configuration" "recovery" {
  count  = local.keep_recovery ? 1 : 0
  bucket = aws_s3_bucket.archive.id
  role   = aws_iam_role.replication[0].arn
  dynamic "rule" {
    for_each = local.recovery_prefixes
    content {
      id       = rule.value
      priority = rule.key + 1
      status   = "Enabled"
      filter { prefix = "${rule.value}/" }
      delete_marker_replication { status = "Disabled" }
      destination {
        bucket        = local.recovery_arn
        storage_class = "STANDARD"
      }
    }
  }
  depends_on = [
    aws_iam_role_policy.replication, aws_s3_bucket_versioning.archive, aws_s3_bucket_versioning.recovery,
    aws_s3_bucket_ownership_controls.recovery, aws_s3_bucket_server_side_encryption_configuration.recovery,
    aws_s3_bucket_policy.recovery,
  ]
}

resource "aws_iam_role_policy" "recovery_verifier" {
  count = local.keep_recovery ? 1 : 0
  name  = "compare-archive-and-recovery"
  role  = aws_iam_role.github["recovery"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["s3:GetObject", "s3:GetObjectVersion"]
      Resource = flatten([for bucket in [local.bucket_arn, local.recovery_arn] : [
        for p in local.recovery_prefixes : "${bucket}/${p}/*"
      ]])
    }]
  })
}

output "recovery_bucket" { value = one(aws_s3_bucket.recovery[*].bucket) }
output "recovery_verifier_role_arn" { value = try(aws_iam_role.github["recovery"].arn, null) }
