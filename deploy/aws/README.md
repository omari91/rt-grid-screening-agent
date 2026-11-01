# Enterprise AWS Audit Lakehouse Blueprints (`deploy/aws/`)

> **Optional Reference Infrastructure — Zero Core Runtime Dependencies**:
> The core `rt-grid-screening-agent` package and `<300 MB` Docker image run **100% standalone** with zero AWS SDK, `boto3`, `pandas`, or `awswrangler` dependencies. The blueprints in this folder are provided for utility/TSO teams deploying immutable regulatory archival at petabyte scale.

## Architecture Overview

1. **Transactional Ledger with 7-Day TTL (`aws_dynamodb_table.audit_receipts`)**:
   - Partition Key: `network_id`, Sort Key: `timestamp_utc`, TTL attribute: `expires_at_epoch` (`int(time.time()) + 604800` injected by `grid_screening_agent.audit_schema.to_dynamodb_audit_item`).
   - Precision-safe float serialization via `Decimal(str(x))`.
2. **Stream Resilience & Poison-Pill Isolation (DynamoDB Streams + SQS DLQ)**:
   - Event Source Mapping enforces `bisect_batch_on_function_error = true`, `maximum_retry_attempts = 3`, and routes failed shard sequence envelopes to `grid-screening-archiver-dlq` (14-day retention).
   - [`replay_dlq.py`](replay_dlq.py) replays quarantined records via `AT_SEQUENCE_NUMBER` after remediation.
3. **Immutable 7-Year WORM Parquet Archival ([`stream_archiver/app.py`](stream_archiver/app.py))**:
   - Packaged dynamically via Terraform's `data "archive_file" "archiver_lambda_zip"` (`output_base64sha256` drift detection) and executed on the AWS-managed `AWSSDKPandas-Python311` Lambda Layer.
   - Explicitly ignores DynamoDB TTL `'REMOVE'` tombstones, extracts Hive partitions (`year=/month=/day=`), and writes Snappy Parquet with `schema_evolution=True` into an S3 Object Lock **`COMPLIANCE` mode (7-year WORM)** bucket (`object_lock_enabled = true`).
4. **Cost-Bounded Regulatory SQL (Athena Workgroup + Scoped IAM + CloudWatch Alarm)**:
   - `tso_analytics_workgroup` enforces a non-overridable **10 GB (`10,737,418,240` bytes)** per-query scan cutoff (`enforce_workgroup_configuration = true`).
   - `TsoRestrictedAthenaAccess` binds `athena:StartQueryExecution` strictly to `tso_analytics_workgroup`.
   - CloudWatch Alarm (`TSO-Analytics-10GB-Cutoff-Breach`) alerts SNS when `>= 3` Athena queries enter `QueryState = CANCELED` within 5 minutes (`treat_missing_data = notBreaching`).
5. **Keyless GitHub Actions OIDC & Least-Privilege CI/CD ([`main.tf`](main.tf), [`.github/workflows/terraform.yml`](../../.github/workflows/terraform.yml))**:
   - `GitHubActions-TerraformDeployRole` trusts `token.actions.githubusercontent.com` strictly scoped to `repo:omari91/rt-grid-screening-agent:ref:refs/heads/main`.
   - `GitHubActions-GridAuditDeployment` replaces `AdministratorAccess` with prefix-scoped permissions and restricts `iam:PassRole` via `iam:PassedToService = lambda.amazonaws.com` on `GridAuditArchiverRole`.

## Contents

- [`bootstrap/main.tf`](bootstrap/main.tf) — One-time prerequisite S3 remote state bucket (`prevent_destroy = true`) and DynamoDB `LockID` mutex table.
- [`main.tf`](main.tf) — Unified Terraform HCL stack (7-year WORM S3 Object Lock, DynamoDB + TTL + Stream, SQS DLQ, `archive_file` Data Wrangler Lambda, Glue Parquet Table, 10 GB Athena Workgroup, `TsoRestrictedAthenaAccess`, CloudWatch SNS Alarm, and least-privilege GitHub OIDC role).
- [`stream_archiver/app.py`](stream_archiver/app.py) — DynamoDB Stream → Snappy S3 Parquet + Glue Catalog Lambda handler (filtering TTL `REMOVE` tombstones).
- [`replay_dlq.py`](replay_dlq.py) — SQS Dead-Letter Queue shard-iterator replay utility.
- [`cdk/audit_lakehouse_stack.py`](cdk/audit_lakehouse_stack.py) — Equivalent AWS CDK (Python) reference stack.
