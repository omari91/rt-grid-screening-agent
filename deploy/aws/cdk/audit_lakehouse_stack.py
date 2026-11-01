"""Reference AWS CDK (Python) stack for the Serverless Grid Screening Audit Lakehouse.

Provisions:
    1. GitHub Actions OIDC Provider + `GitHubActions-GlueReadOnlyRole` locked to `omari91/rt-grid-screening-agent`.
    2. AWS Glue Catalog Database (`grid_audit_db`) + Hive-partitioned Snappy Parquet Table.
    3. Amazon Athena `tso_analytics_workgroup` with enforced 10 GB (`10,737,418,240` bytes) cutoff.
    4. `TsoRestrictedAthenaAccess` Managed IAM Policy blocking `primary` workgroup bypasses.
    5. CloudWatch Alarm (`QueryState = CANCELED`, `treat_missing_data = NOT_BREACHING`) + SNS Topic.
"""

from __future__ import annotations

from aws_cdk import (  # type: ignore[import-not-found]
    Duration,
    Stack,
    aws_athena as athena,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_glue as glue,
    aws_iam as iam,
    aws_sns as sns,
)
from constructs import Construct  # type: ignore[import-not-found]


class GridAuditLakehouseStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        archive_bucket_name: str,
        results_bucket_name: str,
        github_repo: str = "omari91/rt-grid-screening-agent",
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # 1. Glue Database & Partitioned Parquet Table
        database = glue.CfnDatabase(
            self,
            "GridAuditDatabase",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name="grid_audit_db",
                description="Analytical database for cryptographically signed grid screening audit receipts",
            ),
        )

        table = glue.CfnTable(
            self,
            "GridAuditParquetTable",
            catalog_id=self.account,
            database_name="grid_audit_db",
            table_input=glue.CfnTable.TableInputProperty(
                name="grid_audit_receipts_parquet",
                table_type="EXTERNAL_TABLE",
                parameters={"classification": "parquet"},
                partition_keys=[
                    glue.CfnTable.ColumnProperty(name="year", type="string"),
                    glue.CfnTable.ColumnProperty(name="month", type="string"),
                    glue.CfnTable.ColumnProperty(name="day", type="string"),
                ],
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=f"s3://{archive_bucket_name}/receipts_parquet/",
                    input_format="org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe",
                        parameters={"serialization.format": "1"},
                    ),
                    columns=[
                        glue.CfnTable.ColumnProperty(name="receipt_id", type="string"),
                        glue.CfnTable.ColumnProperty(name="network_id", type="string"),
                        glue.CfnTable.ColumnProperty(name="decision", type="string"),
                        glue.CfnTable.ColumnProperty(name="reproducibility_sha256", type="string"),
                    ],
                ),
            ),
        )
        table.add_dependency(database)

        # 2. Athena Workgroup with Enforced 10 GB Per-Query Scan Cutoff
        athena.CfnWorkGroup(
            self,
            "TsoAnalyticsWorkGroup",
            name="tso_analytics_workgroup",
            description="Workgroup for TSO regulatory reporting with hard 10 GB scan limit.",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=True,
                bytes_scanned_cutoff_per_query=10_737_418_240,
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{results_bucket_name}/athena_tso_results/",
                ),
            ),
        )

        # 3. Scoped TSO IAM Policy
        tso_policy = iam.ManagedPolicy(
            self,
            "TsoAthenaWorkgroupPolicy",
            managed_policy_name="TsoRestrictedAthenaAccess",
            description="Forces TSO users into tso_analytics_workgroup and grants read-only Glue/S3 access.",
        )
        tso_policy.add_statements(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "athena:StartQueryExecution",
                    "athena:StopQueryExecution",
                    "athena:GetQueryExecution",
                    "athena:GetQueryResults",
                    "athena:GetWorkGroup",
                ],
                resources=[f"arn:aws:athena:{self.region}:{self.account}:workgroup/tso_analytics_workgroup"],
            ),
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["glue:GetDatabase", "glue:GetTable", "glue:GetPartitions"],
                resources=[
                    f"arn:aws:glue:{self.region}:{self.account}:catalog",
                    f"arn:aws:glue:{self.region}:{self.account}:database/grid_audit_db",
                    f"arn:aws:glue:{self.region}:{self.account}:table/grid_audit_db/grid_audit_receipts_parquet",
                ],
            ),
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[f"arn:aws:s3:::{archive_bucket_name}", f"arn:aws:s3:::{archive_bucket_name}/*"],
            ),
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"],
                resources=[f"arn:aws:s3:::{results_bucket_name}", f"arn:aws:s3:::{results_bucket_name}/*"],
            ),
        )

        # 4. Keyless GitHub Actions OIDC Role for CI Schema Checks
        github_provider = iam.OpenIdConnectProvider(
            self,
            "GitHubProvider",
            url="https://token.actions.githubusercontent.com",
            client_ids=["sts.amazonaws.com"],
        )
        github_role = iam.Role(
            self,
            "GitHubActionsGlueRole",
            role_name="GitHubActions-GlueReadOnlyRole",
            assumed_by=iam.OpenIdConnectPrincipal(
                github_provider,
                conditions={
                    "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
                    "StringLike": {"token.actions.githubusercontent.com:sub": f"repo:{github_repo}:*"},
                },
            ),
        )
        github_role.add_to_policy(
            iam.PolicyStatement(
                actions=["glue:GetDatabase", "glue:GetTable"],
                resources=[
                    f"arn:aws:glue:{self.region}:{self.account}:catalog",
                    f"arn:aws:glue:{self.region}:{self.account}:database/grid_audit_db",
                    f"arn:aws:glue:{self.region}:{self.account}:table/grid_audit_db/grid_audit_receipts_parquet",
                ],
            )
        )

        # 5. CloudWatch Alarm + SNS Alert on Repeated 10 GB Cutoff Cancellations
        alert_topic = sns.Topic(self, "TsoLimitAlertTopic", topic_name="tso-workgroup-limit-alerts")
        cancelled_metric = cloudwatch.Metric(
            namespace="AWS/Athena",
            metric_name="TotalExecutionTime",
            dimensions_map={"WorkGroup": "tso_analytics_workgroup", "QueryState": "CANCELED"},
            statistic="SampleCount",
            period=Duration.minutes(5),
        )
        alarm = cloudwatch.Alarm(
            self,
            "TsoQueryCutoffAlarm",
            alarm_name="TSO-Analytics-10GB-Cutoff-Breach",
            alarm_description="Alerts engineering when TSO queries are repeatedly cancelled by the 10 GB cutoff.",
            metric=cancelled_metric,
            threshold=3,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(cw_actions.SnsAction(alert_topic))

