"""CI guardrail tests for forward-only Parquet/Athena schema evolution and DynamoDB Decimal persistence."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from grid_screening_agent.agent import GridScreeningAgent
from grid_screening_agent.audit_schema import (
    BASELINE_LAKEHOUSE_COLUMN_TYPES,
    flatten_verdict_for_parquet,
    float_to_dynamodb_decimal,
    infer_athena_column_types,
    to_dynamodb_audit_item,
)
from grid_screening_agent.models import AssetType, ConnectionApplication, ScreeningVerdict


def _contains_raw_float(obj: Any) -> bool:
    if isinstance(obj, float):
        return True
    if isinstance(obj, list):
        return any(_contains_raw_float(x) for x in obj)
    if isinstance(obj, dict):
        return any(_contains_raw_float(v) for v in obj.values())
    return False


def test_parquet_lakehouse_schema_forward_only_compatibility() -> None:
    """Blocks merges if baseline Parquet/Athena columns are removed or change SQL types."""
    agent = GridScreeningAgent(provider="deterministic")
    verdict = agent.screen_application(
        ConnectionApplication(
            network_id="R",
            asset_type=AssetType.ROOFTOP_PV,
            proposed_capacity_kw=3.0,
            feeder_background_scale=3.0,
        )
    )
    flattened = flatten_verdict_for_parquet(verdict)
    proposed_schema = infer_athena_column_types(flattened)

    active_columns = set(BASELINE_LAKEHOUSE_COLUMN_TYPES.keys())
    proposed_columns = set(proposed_schema.keys())

    missing_columns = active_columns - proposed_columns
    assert not missing_columns, (
        f"DESTRUCTIVE CHANGE: Columns removed from lakehouse schema: {missing_columns}. "
        "Parquet schema evolution requires strict forward-addition."
    )

    for col in active_columns:
        assert proposed_schema[col] == BASELINE_LAKEHOUSE_COLUMN_TYPES[col], (
            f"DESTRUCTIVE CHANGE: Column '{col}' changed Athena type from "
            f"'{BASELINE_LAKEHOUSE_COLUMN_TYPES[col]}' to '{proposed_schema[col]}'."
        )


def test_dynamodb_decimal_serialization_is_precision_exact_and_roundtrips() -> None:
    """Proves float -> Decimal(str(x)) avoids IEEE-754 drift and round-trips into ScreeningVerdict."""
    assert float_to_dynamodb_decimal(800.1) == Decimal("800.1")

    agent = GridScreeningAgent(provider="deterministic")
    verdict = agent.screen_application(
        ConnectionApplication(
            network_id="BrokenHill_132kV",
            asset_type=AssetType.HYPERSCALE_DATACENTRE,
            proposed_capacity_kw=150_000.0,
        )
    )
    ddb_item = to_dynamodb_audit_item(verdict)
    assert ddb_item["pk_network_id"] == "NETWORK#BrokenHill_132kV"
    assert ddb_item["sk_timestamp_utc"] == verdict.audit_receipt.timestamp_utc
    assert ddb_item["network_id"] == "BrokenHill_132kV"
    assert isinstance(ddb_item["expires_at_epoch"], int)
    assert _contains_raw_float(ddb_item) is False

    # Strip DynamoDB routing keys and validate clean Pydantic deserialization from Decimals
    clean_copy = {k: v for k, v in ddb_item.items() if k not in ("pk_network_id", "sk_timestamp_utc")}
    restored = ScreeningVerdict.model_validate(clean_copy)
    assert restored.audit_receipt.reproducibility_sha256 == verdict.audit_receipt.reproducibility_sha256
    assert restored.decision == verdict.decision

