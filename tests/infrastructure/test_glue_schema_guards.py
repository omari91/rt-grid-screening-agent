"""CI guardrail tests for the AWS Glue/Athena Parquet schema contract and DynamoDB Stream unmarshaling."""

from __future__ import annotations

from typing import Any

import numpy as np

from deploy.aws.stream_archiver.app import (
    extract_partitioned_records,
    unmarshal_dynamodb_image,
)
from grid_screening_agent.agent import GridScreeningAgent
from grid_screening_agent.audit_schema import to_dynamodb_audit_item
from grid_screening_agent.intake import GridConnectionIntent
from grid_screening_agent.models import AssetType, ConnectionApplication

try:
    import pandas as pd  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Core <300 MB venv omits pandas; CI installs .[test] with pandas
    pd = None

# Strict analytical column contract relied upon by AWS Glue and TSO Athena BI workgroups
REQUIRED_ANALYTICAL_COLUMNS: dict[str, str] = {
    "network_id": "object",
    "timestamp_utc": "object",
    "decision": "object",
    "capacity_kw": "float64",
    "worst_case_voltage_pu": "float64",
    "year": "object",
    "month": "object",
    "day": "object",
}


def _infer_column_dtype(values: list[Any]) -> str:
    """Match Pandas column dtype naming (`object` for strings, `float64` for floats)."""
    if pd is not None:
        df = pd.DataFrame({"col": values})
        return str(df["col"].dtype)
    if all(isinstance(v, str) for v in values):
        return "object"
    arr = np.asarray(values)
    return "object" if arr.dtype.kind in ("U", "S", "O") else str(arr.dtype)


def _to_dynamodb_wire_image(flat_item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Encode primitives into DynamoDB Stream `NewImage` AttributeValue syntax."""
    wire: dict[str, dict[str, Any]] = {}
    for key, val in flat_item.items():
        if isinstance(val, bool):
            wire[key] = {"BOOL": val}
        elif isinstance(val, str):
            wire[key] = {"S": val}
        elif isinstance(val, (int, float)) or hasattr(val, "as_tuple"):
            wire[key] = {"N": str(val)}
    return wire


def test_dataframe_schema_matches_glue_contract_and_filters_ttl_remove_events() -> None:
    """Proves live ScreeningVerdict -> DynamoDB Stream -> Unmarshaler matches the AWS Glue Parquet contract."""
    intent = GridConnectionIntent(capacity_kw=200.0, voltage_level="LV", asset_type="generator")
    app = intent.to_connection_application().model_copy(
        update={"network_id": "R", "proposed_capacity_kw": 3.0, "feeder_background_scale": 3.0}
    )
    verdict = GridScreeningAgent(provider="deterministic").screen_application(app)
    ddb_item = to_dynamodb_audit_item(verdict)
    assert isinstance(ddb_item["expires_at_epoch"], int)

    stream_event = {
        "Records": [
            # 1. Valid INSERT record from live ScreeningVerdict
            {
                "eventName": "INSERT",
                "dynamodb": {"NewImage": _to_dynamodb_wire_image(ddb_item)},
            },
            # 2. Ghost REMOVE tombstone emitted by DynamoDB 7-day TTL purge (must be ignored!)
            {
                "eventName": "REMOVE",
                "dynamodb": {
                    "OldImage": {
                        "network_id": {"S": "R"},
                        "timestamp_utc": {"S": "2026-10-24T12:00:00Z"},
                    }
                },
            },
        ]
    }

    parsed_rows = extract_partitioned_records(stream_event)
    assert len(parsed_rows) == 1, "TTL REMOVE tombstone must be filtered out before Parquet archival!"
    row = parsed_rows[0]

    if pd is not None:
        df = pd.DataFrame(parsed_rows)
        for col_name, expected_dtype in REQUIRED_ANALYTICAL_COLUMNS.items():
            assert col_name in df.columns, f"FATAL: Required Glue column '{col_name}' missing."
            actual_dtype = str(df[col_name].dtype)
            assert actual_dtype == expected_dtype, (
                f"FATAL: Type mismatch for '{col_name}'. Expected {expected_dtype}, got {actual_dtype}."
            )
    else:
        for col_name, expected_dtype in REQUIRED_ANALYTICAL_COLUMNS.items():
            assert col_name in row, f"FATAL: Required Glue column '{col_name}' missing."
            actual_dtype = _infer_column_dtype([row[col_name]])
            assert actual_dtype == expected_dtype, (
                f"FATAL: Type mismatch for '{col_name}'. Expected {expected_dtype}, got {actual_dtype}."
            )


def test_unmarshal_dynamodb_image_converts_numeric_decimals_to_floats_and_epoch_to_int() -> None:
    raw_ddb = {
        "network_id": {"S": "BrokenHill_132kV"},
        "timestamp_utc": {"S": "2026-10-31T15:54:00Z"},
        "capacity_kw": {"N": "150000.0"},
        "decision": {"S": "ESCALATE_DETAILED_STUDY"},
        "worst_case_voltage_pu": {"N": "0.9412"},
        "expires_at_epoch": {"N": "1794066840"},
    }
    item = unmarshal_dynamodb_image(raw_ddb)
    assert item["network_id"] == "BrokenHill_132kV"
    assert isinstance(item["capacity_kw"], float) and item["capacity_kw"] == 150000.0
    assert isinstance(item["worst_case_voltage_pu"], float)
    assert isinstance(item["expires_at_epoch"], int) and item["expires_at_epoch"] == 1794066840

    # Confirm ConnectionApplication compatibility for HV assets
    hv_app = ConnectionApplication(
        network_id=item["network_id"],
        asset_type=AssetType.HYPERSCALE_DATACENTRE,
        proposed_capacity_kw=item["capacity_kw"],
    )
    assert hv_app.proposed_capacity_kw == 150000.0

