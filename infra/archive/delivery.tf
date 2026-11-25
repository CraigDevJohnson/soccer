locals {
  oidc_host  = "token.actions.githubusercontent.com"
  oidc_owner = "repo:CraigDevJohnson@42454849/soccer@1412575682:environment:"
}

resource "aws_iam_role" "github" {
  for_each = toset(concat(["activation", "image", "import", "release", "run"], local.keep_recovery ? ["recovery"] : []))
  name     = "${local.prefix}-${each.key}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = "arn:aws:iam::${var.account_id}:oidc-provider/${local.oidc_host}" }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_host}:aud" = "sts.amazonaws.com"
          "${local.oidc_host}:sub" = "${local.oidc_owner}archive-${each.key}-${var.site_environment}"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "image" {
  name = "publish-worker-image"
  role = aws_iam_role.github["image"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:DescribeImages"]
        Resource = aws_ecr_repository.worker.arn
      }
      ], var.site_environment == "prod" ? [
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability", "ecr:DescribeImages"]
        Resource = local.dev_repo_arn
      },
      {
        Effect   = "Allow"
        Action   = ["lambda:GetFunction"]
        Resource = local.dev_worker_arn
      }
    ] : [])
  })
}

resource "aws_iam_role_policy" "import" {
  name = "verify-and-import-baseline"
  role = aws_iam_role.github["import"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = ["${local.bucket_arn}/staging/*", "${local.bucket_arn}/generations/*", "${local.bucket_arn}/control/active.json"]
      },
      {
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = ["${local.bucket_arn}/generations/*", "${local.bucket_arn}/control/active.json"]
        # Initial import can never reset a pointer after hosted collection advances.
        Condition = { StringEquals = { "s3:if-none-match" = "*" } }
      }
    ]
  })
  # S3 live replication only covers versions written after its rules exist.
  depends_on = [aws_s3_bucket_replication_configuration.recovery]
}

resource "aws_iam_role" "stage" {
  count = var.staging_access == null ? 0 : 1
  name  = "${local.prefix}-stage"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { AWS = var.staging_access.principal_arn }
    }]
  })
}

resource "aws_iam_role_policy" "stage" {
  count = var.staging_access == null ? 0 : 1
  name  = "stage-approved-baseline"
  role  = aws_iam_role.stage[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:PutObject"]
      Resource = [for file in ["archive.tar.gz", "manifest.json"] : "${local.bucket_arn}/staging/${var.staging_access.generation}/${file}"]
    }]
  })
}

resource "aws_iam_role_policy" "release" {
  name = "release-worker-image"
  role = aws_iam_role.github["release"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect   = "Allow"
        Action   = ["lambda:UpdateFunctionCode", "lambda:GetFunctionConfiguration", "lambda:GetFunction"]
        Resource = local.worker_arn
      },
      {
        Effect   = "Allow"
        Action   = ["ecr:GetRepositoryPolicy", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = aws_ecr_repository.worker.arn
      }
      ], var.site_environment == "prod" ? [
      {
        Effect   = "Allow"
        Action   = ["lambda:GetFunction"]
        Resource = local.dev_worker_arn
      }
    ] : [])
  })
}

resource "aws_iam_role_policy" "run" {
  name = "invoke-worker"
  role = aws_iam_role.github["run"].id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = local.worker_arn }]
  })
}

resource "aws_iam_role_policy" "activation" {
  name = "select-collection-mode"
  role = aws_iam_role.github["activation"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${local.bucket_arn}/${local.settings_key}"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = [for key in local.handoff_keys : "${local.peer_bucket}/${key}"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = local.peer_bucket
        Condition = {
          StringEquals          = { "s3:prefix" = local.handoff_keys }
          NumericLessThanEquals = { "s3:max-keys" = "1" }
        }
      }
      ], [for statement in [
        {
          Effect   = "Allow"
          Action   = ["scheduler:GetSchedule", "scheduler:UpdateSchedule"]
          Resource = "arn:aws:scheduler:us-west-2:${var.account_id}:schedule/${local.prefix}/${local.daily.Name}"
        },
        {
          Effect    = "Allow"
          Action    = ["iam:PassRole"]
          Resource  = "arn:aws:iam::${var.account_id}:role/${local.prefix}-scheduler"
          Condition = { StringEquals = { "iam:PassedToService" = "scheduler.amazonaws.com" } }
        }
    ] : statement if var.site_environment == "prod"])
  })
}
