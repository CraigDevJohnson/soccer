resource "aws_s3_bucket" "archive" {
  bucket        = local.bucket_name
  force_destroy = false
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_public_access_block" "archive" {
  bucket                  = aws_s3_bucket.archive.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "archive" {
  bucket = aws_s3_bucket.archive.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_versioning" "archive" {
  bucket = aws_s3_bucket.archive.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
  rule {
    id     = "superseded-control-records"
    status = "Enabled"
    filter { prefix = "control/" }
    noncurrent_version_expiration { noncurrent_days = 30 }
  }
  depends_on = [aws_s3_bucket_versioning.archive]
}

resource "aws_s3_bucket_policy" "archive" {
  bucket = aws_s3_bucket.archive.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "RequireTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [local.bucket_arn, "${local.bucket_arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false", "aws:PrincipalIsAWSService" = "false" } }
      },
      {
        Sid       = "CreateImmutableObjectsOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = [for prefix in ["staging", "generations", "journal"] : "${local.bucket_arn}/${prefix}/*"]
        Condition = {
          Bool = { "s3:ObjectCreationOperation" = "true" }
          Null = { "s3:if-none-match" = "true" }
        }
      },
      {
        Sid       = "CompareOrCreateControlRecords"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = ["${local.bucket_arn}/control/*", "${local.bucket_arn}/${local.settings_key}"]
        Condition = {
          Bool = { "s3:ObjectCreationOperation" = "true" }
          Null = { "s3:if-none-match" = "true", "s3:if-match" = "true" }
        }
      }
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.archive]
}

resource "aws_ecr_repository" "worker" {
  name                 = local.worker_name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}

resource "aws_ecr_repository_policy" "worker" {
  repository = aws_ecr_repository.worker.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "LambdaImageRetrieval"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
      Condition = { ArnLike = { "aws:SourceArn" = [local.worker_arn, "${local.worker_arn}:*"] } }
    }]
  })
}
