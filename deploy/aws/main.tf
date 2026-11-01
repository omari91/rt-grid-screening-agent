terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }

  # Remote state backend with DynamoDB mutex locking (provision prerequisites via ./bootstrap/main.tf first)
  backend "s3" {
    bucket         = "grid-agent-tf-state-omari91"
    key            = "audit-lakehouse/terraform.tfstate"
    region         = "eu-central-1"
    dynamodb_table = "grid-agent-tf-locks"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  type        = string
  default     = "eu-central-1"
  description = "AWS region for the Grid Screening Audit Lakehouse."
}

variable "github_repository" {
  type        = string
  default     = "omari91/rt-grid-screening-agent"
  description = "GitHub repository permitted to assume the keyless OIDC Terraform deployment role."
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# --- 1. IMMUTABLE WORM STORAGE: S3 BUCKETS & 7-YEAR OBJECT LOCK ---

resource "aws_s3_bucket" "archive" {
  bucket              = "grid-audit-archive-${data.aws_caller_identity.current.account_id}"
  force_destroy       = false
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "archive_versioning" {
  bucket = aws_s3_bucket.archive.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "archive_crypto" {
  bucket = aws_s3_bucket.archive.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "archive_block_public" {
  bucket                  = aws_s3_bucket.archive.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_object_lock_configuration" "archive_lock" {
  bucket = aws_s3_bucket.archive.id

  rule {
    default_retention {
      mode  = "COMPLIANCE"
      years = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.archive_versioning]
}

resource "aws_s3_bucket" "athena_results" {
  bucket        = "grid-athena-results-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "athena_results_block_public" {
  bucket                  = aws_s3_bucket.athena_results.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# --- 2. TRANSACTIONAL LEDGER: DYNAMODB WITH 7-DAY TTL & SQS DLQ ---

resource "aws_dynamodb_table" "audit_receipts" {
  name             = "grid-screening-audit-receipts"
  billing_mode     = "PAY_PER_REQUEST"
  hash_key         = "network_id"
  range_key        = "timestamp_utc"
  stream_enabled   = true
  stream_view_type = "NEW_IMAGE"

  attribute {
    name = "network_id"
    type = "S"
  }

  attribute {
    name = "timestamp_utc"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at_epoch"
    enabled        = true
  }
}

resource "aws_sqs_queue" "dlq" {
  name                      = "grid-screening-archiver-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
}

# --- 3. STREAM ARCHIVER COMPUTE: DYNAMIC ZIP + DATA WRANGLER LAMBDA ---

data "archive_file" "archiver_lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/stream_archiver"
  output_path = "${path.module}/.terraform/tmp/stream_archiver_payload.zip"
}

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "archiver_lambda_role" {
  name               = "GridAuditArchiverRole"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy" "archiver_permissions" {
  name = "GridAuditArchiverPermissions"
  role = aws_iam_role.archiver_lambda_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:ListStreams"]
        Resource = aws_dynamodb_table.audit_receipts.stream_arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetBucketLocation", "s3:ListBucket"]
        Resource = [aws_s3_bucket.archive.arn, "${aws_s3_bucket.archive.arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["glue:CreateTable", "glue:UpdateTable", "glue:GetTable", "glue:GetDatabase", "glue:BatchCreatePartition", "glue:GetPartitions"]
        Resource = ["arn:aws:glue:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:*"]
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.dlq.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:*"
      }
    ]
  })
}

resource "aws_lambda_function" "archiver" {
  function_name    = "GridAuditArchiver"
  role             = aws_iam_role.archiver_lambda_role.arn
  handler          = "app.lambda_handler"
  runtime          = "python3.11"
  timeout          = 60
  memory_size      = 512
  filename         = data.archive_file.archiver_lambda_zip.output_path
  source_code_hash = data.archive_file.archiver_lambda_zip.output_base64sha256

  # Official AWS SDK for pandas (awswrangler) Lambda Layer for Python 3.11
  layers = ["arn:aws:lambda:${data.aws_region.current.name}:336392948345:layer:AWSSDKPandas-Python311:12"]

  environment {
    variables = {
      ARCHIVE_BUCKET = aws_s3_bucket.archive.bucket
      GLUE_DATABASE  = aws_glue_catalog_database.grid_audit_db.name
      GLUE_TABLE     = aws_glue_catalog_table.grid_audit_parquet_table.name
    }
  }
}

resource "aws_lambda_event_source_mapping" "dynamo_stream" {
  event_source_arn               = aws_dynamodb_table.audit_receipts.stream_arn
  function_name                  = aws_lambda_function.archiver.arn
  starting_position              = "LATEST"
  batch_size                     = 100
  maximum_retry_attempts         = 3
  maximum_record_age_in_seconds  = 3600
  bisect_batch_on_function_error = true

  destination_config {
    on_failure {
      destination_arn = aws_sqs_queue.dlq.arn
    }
  }
}

# --- 4. ANALYTICS: GLUE PARQUET CATALOG, 10 GB ATHENA WORKGROUP & ALARM ---

resource "aws_glue_catalog_database" "grid_audit_db" {
  name        = "grid_audit_db"
  description = "Analytical database for immutable grid screening audit receipts."
}

resource "aws_glue_catalog_table" "grid_audit_parquet_table" {
  name          = "grid_audit_receipts_parquet"
  database_name = aws_glue_catalog_database.grid_audit_db.name
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    "classification"  = "parquet"
    "compressionType" = "snappy"
  }

  partition_keys {
    name = "year"
    type = "string"
  }
  partition_keys {
    name = "month"
    type = "string"
  }
  partition_keys {
    name = "day"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.archive.bucket}/receipts_parquet/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    serde_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
      parameters            = { "serialization.format" = "1" }
    }

    columns {
      name = "receipt_id"
      type = "string"
    }
    columns {
      name = "network_id"
      type = "string"
    }
    columns {
      name = "timestamp_utc"
      type = "string"
    }
    columns {
      name = "capacity_kw"
      type = "double"
    }
    columns {
      name = "decision"
      type = "string"
    }
    columns {
      name = "worst_case_voltage_pu"
      type = "double"
    }
    columns {
      name = "reproducibility_sha256"
      type = "string"
    }
  }
}

resource "aws_athena_workgroup" "tso_analytics" {
  name        = "tso_analytics_workgroup"
  description = "Workgroup for TSO regulatory reporting with strict 10 GB scan cutoff."
  state       = "ENABLED"

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    bytes_scanned_cutoff_per_query     = 10737418240 # 10 GB hard per-query cutoff

    result_configuration {
      output_location = "s3://${aws_s3_bucket.athena_results.bucket}/tso_queries/"
    }
  }
}

resource "aws_sns_topic" "athena_cutoff_alerts" {
  name = "tso-athena-10gb-cutoff-alerts"
}

resource "aws_cloudwatch_metric_alarm" "athena_cutoff_breach" {
  alarm_name          = "TSO-Analytics-10GB-Cutoff-Breach"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "TotalExecutionTime"
  namespace           = "AWS/Athena"
  period              = 300
  statistic           = "SampleCount"
  threshold           = 3
  treat_missing_data  = "notBreaching"
  alarm_description   = "Triggers when >= 3 TSO Athena queries are CANCELED (e.g., 10 GB cutoff breach) within 5 minutes."
  alarm_actions       = [aws_sns_topic.athena_cutoff_alerts.arn]

  dimensions = {
    WorkGroup  = aws_athena_workgroup.tso_analytics.name
    QueryState = "CANCELED"
  }
}

# --- 5. ENFORCEMENT: TSO RESTRICTED ATHENA IAM POLICY ---

resource "aws_iam_policy" "tso_restricted_athena" {
  name        = "TsoRestrictedAthenaAccess"
  description = "Forces TSO users into the restricted 10 GB Athena workgroup."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["athena:StartQueryExecution", "athena:StopQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults", "athena:GetWorkGroup"]
        Resource = aws_athena_workgroup.tso_analytics.arn
      },
      {
        Effect = "Allow"
        Action = ["glue:GetDatabase", "glue:GetTable", "glue:GetPartitions"]
        Resource = [
          "arn:aws:glue:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:catalog",
          aws_glue_catalog_database.grid_audit_db.arn,
          aws_glue_catalog_table.grid_audit_parquet_table.arn
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.archive.arn, "${aws_s3_bucket.archive.arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"]
        Resource = [aws_s3_bucket.athena_results.arn, "${aws_s3_bucket.athena_results.arn}/*"]
      }
    ]
  })
}

# --- 6. KEYLESS GITHUB ACTIONS OIDC & LEAST-PRIVILEGE DEPLOYMENT ROLE ---

resource "aws_iam_openid_connect_provider" "github_actions" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["1b511abead59c6ce207077c0bf0e0043b1382612"]
}

data "aws_iam_policy_document" "github_actions_trust_policy" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github_actions.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "github_actions_deploy_role" {
  name               = "GitHubActions-TerraformDeployRole"
  assume_role_policy = data.aws_iam_policy_document.github_actions_trust_policy.json
}

data "aws_iam_policy_document" "lakehouse_deployment_permissions" {
  # 1. State Backend & Mutex Lock Access
  statement {
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetObject",
      "s3:PutObject",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:DeleteItem"
    ]
    resources = [
      "arn:aws:s3:::grid-agent-tf-state-omari91",
      "arn:aws:s3:::grid-agent-tf-state-omari91/*",
      "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/grid-agent-tf-locks"
    ]
  }

  # 2. Scoped Serverless Compute, Storage & Monitoring Provisioning
  statement {
    effect = "Allow"
    actions = [
      "s3:CreateBucket", "s3:DeleteBucket", "s3:PutBucket*", "s3:Get*", "s3:List*", "s3:PutObjectLockConfiguration", "s3:GetObjectLockConfiguration",
      "dynamodb:CreateTable", "dynamodb:DeleteTable", "dynamodb:UpdateTable", "dynamodb:UpdateTimeToLive", "dynamodb:Describe*", "dynamodb:TagResource", "dynamodb:UntagResource",
      "sqs:CreateQueue", "sqs:DeleteQueue", "sqs:GetQueueAttributes", "sqs:SetQueueAttributes", "sqs:TagQueue", "sqs:ListQueues",
      "lambda:CreateFunction", "lambda:DeleteFunction", "lambda:UpdateFunction*", "lambda:Get*", "lambda:List*", "lambda:CreateEventSourceMapping", "lambda:DeleteEventSourceMapping", "lambda:GetEventSourceMapping", "lambda:UpdateEventSourceMapping",
      "sns:CreateTopic", "sns:DeleteTopic", "sns:GetTopicAttributes", "sns:SetTopicAttributes", "sns:TagResource",
      "cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms", "cloudwatch:DescribeAlarms", "cloudwatch:TagResource"
    ]
    resources = [
      "arn:aws:s3:::grid-*",
      "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/grid-*",
      "arn:aws:sqs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:grid-*",
      "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:GridAudit*",
      "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:event-source-mapping:*",
      "arn:aws:sns:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:tso-*",
      "arn:aws:cloudwatch:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:alarm:TSO-*"
    ]
  }

  # 3. Analytics Infrastructure (Glue & Athena)
  statement {
    effect = "Allow"
    actions = [
      "glue:CreateDatabase", "glue:GetDatabase", "glue:DeleteDatabase",
      "glue:CreateTable", "glue:GetTable", "glue:DeleteTable", "glue:UpdateTable",
      "athena:CreateWorkGroup", "athena:DeleteWorkGroup", "athena:GetWorkGroup", "athena:UpdateWorkGroup", "athena:TagResource"
    ]
    resources = [
      "arn:aws:glue:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:*",
      "arn:aws:athena:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:workgroup/tso_analytics_workgroup"
    ]
  }

  # 4. Scoped IAM Role & Policy Provisioning
  statement {
    effect = "Allow"
    actions = [
      "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateRole",
      "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRolePolicy",
      "iam:CreatePolicy", "iam:DeletePolicy", "iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListPolicyVersions"
    ]
    resources = [
      "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/GridAudit*",
      "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/TsoRestrictedAthenaAccess",
      "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/GitHubActions-GridAuditDeployment"
    ]
  }

  # 5. CRITICAL: Strictly service-bound iam:PassRole (Lambda only; blocks privilege escalation)
  statement {
    effect  = "Allow"
    actions = ["iam:PassRole"]
    resources = [
      "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/GridAuditArchiverRole"
    ]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_policy" "lakehouse_deployment_policy" {
  name        = "GitHubActions-GridAuditDeployment"
  description = "Least-privilege deployment policy for the AWS Audit Lakehouse."
  policy      = data.aws_iam_policy_document.lakehouse_deployment_permissions.json
}

resource "aws_iam_role_policy_attachment" "github_actions_least_privilege" {
  role       = aws_iam_role.github_actions_deploy_role.name
  policy_arn = aws_iam_policy.lakehouse_deployment_policy.arn
}

