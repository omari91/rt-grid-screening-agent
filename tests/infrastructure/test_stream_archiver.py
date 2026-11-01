"""Lambda archiver tests: DynamoDB unmarshaling, TTL `REMOVE` filtering, and Hive partition extraction.

Why `awswrangler` is injected via `sys.modules` instead of patched as a module attribute:
    The handler imports `awswrangler` lazily (it ships in the AWS-managed `AWSSDKPandas`
    Lambda layer and is deliberately absent from the core <300 MB venv). Injecting a fake
    module lets CI assert the exact `to_parquet` contract without any S3/Glue network calls.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from deploy.aws.stream_archiver.app import lambda_handler, unmarshal_dynamodb_image


def _fake_awswrangler() -> tuple[ModuleType, MagicMock]:
    to_parquet = MagicMock(name="to_parquet")
    fake = ModuleType("awswrangler")
    fake.s3 = SimpleNamespace(to_parquet=to_parquet)  # type: ignore[attr-defined]
    return fake, to_parquet


def test_unmarshal_dynamodb_image_flattens_types() -> None:
    """`N` values are normalized to `float` (not `Decimal`) so Parquet emits DOUBLE, not STRING."""
    parsed = unmarshal_dynamodb_image(
        {
            "network_id": {"S": "R"},
            "capacity_kw": {"N": "250.5"},
            "decision": {"S": "FAST_TRACK_APPROVED"},
            "requires_human_review": {"BOOL": False},
            "binding_constraints": {"L": [{"S": "NONE"}]},
        }
    )
    assert parsed["network_id"] == "R"
    assert isinstance(parsed["capacity_kw"], float) and parsed["capacity_kw"] == 250.5
    assert parsed["decision"] == "FAST_TRACK_APPROVED"
    assert parsed["requires_human_review"] is False
    assert parsed["binding_constraints"] == ["NONE"]


def test_lambda_handler_ignores_ttl_deletions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHIVE_BUCKET", "test-bucket")
    monkeypatch.setenv("GLUE_DATABASE", "test-db")
    monkeypatch.setenv("GLUE_TABLE", "test-table")
    fake_wr, to_parquet = _fake_awswrangler()

    event = {"Records": [{"eventName": "REMOVE", "dynamodb": {"OldImage": {"network_id": {"S": "R"}}}}]}
    with patch.dict(sys.modules, {"awswrangler": fake_wr}):
        response = lambda_handler(event, None)

    assert response["status"] == "skipped"
    assert response["reason"] == "No actionable records"
    to_parquet.assert_not_called()


def test_lambda_handler_processes_inserts_and_extracts_partitions(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pandas")
    monkeypatch.setenv("ARCHIVE_BUCKET", "grid-archive")
    monkeypatch.setenv("GLUE_DATABASE", "grid_db")
    monkeypatch.setenv("GLUE_TABLE", "receipts")
    fake_wr, to_parquet = _fake_awswrangler()

    event = {
        "Records": [
            {
                "eventName": "INSERT",
                "dynamodb": {
                    "NewImage": {
                        "network_id": {"S": "BrokenHill_132kV"},
                        "timestamp_utc": {"S": "2026-10-31T15:54:00Z"},
                        "capacity_kw": {"N": "150000"},
                    }
                },
            }
        ]
    }
    with patch.dict(sys.modules, {"awswrangler": fake_wr}):
        response = lambda_handler(event, None)

    assert response == {"status": "success", "archived_count": 1}
    to_parquet.assert_called_once()
    kwargs = to_parquet.call_args.kwargs
    assert kwargs["database"] == "grid_db"
    assert kwargs["table"] == "receipts"
    assert kwargs["path"] == "s3://grid-archive/receipts_parquet/"
    assert kwargs["partition_cols"] == ["year", "month", "day"]
    assert kwargs["schema_evolution"] is True

    df = kwargs["df"]
    assert len(df) == 1
    row = df.iloc[0]
    assert (row["year"], row["month"], row["day"]) == ("2026", "10", "31")

