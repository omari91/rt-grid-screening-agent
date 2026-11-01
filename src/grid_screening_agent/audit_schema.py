"""Zero-dependency audit-ledger serialization & forward-only Parquet/Athena schema contract.

Why this module lives in Python stdlib (without importing `boto3`, `pandas`, or `awswrangler`):
    1. Container Budget (`<300 MB`): Utility deployments that stream `ScreeningVerdict`
       records to DynamoDB or S3/Athena Parquet archives need canonical flattening and
       precision-safe `Decimal` conversion without bloating the core solver container.
    2. Forward-Only Schema Evolution Gate: `BASELINE_LAKEHOUSE_COLUMN_TYPES` locks the
       top-level columnar contract so CI (`tests/test_schema_evolution.py`) blocks any
       PR that deletes an existing audit column or mutates its Athena SQL type, preventing
       `HIVE_PARTITION_SCHEMA_MISMATCH` and `HIVE_BAD_DATA` errors in historical queries.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

from grid_screening_agent.models import ScreeningVerdict

DEFAULT_AUDIT_TTL_SECONDS: int = 604800  # 7-day transactional DynamoDB retention post-S3 archival

# Locked baseline Athena/Glue column types for forward-only Parquet schema evolution.
# Adding new Optional columns is permitted; deleting or changing any column below fails CI.
BASELINE_LAKEHOUSE_COLUMN_TYPES: dict[str, str] = {
    "pk_network_id": "string",
    "sk_timestamp_utc": "string",
    "timestamp_utc": "string",
    "receipt_id": "string",
    "application_id": "string",
    "network_id": "string",
    "connection_node": "string",
    "voltage_tier": "string",
    "nominal_kv": "double",
    "asset_type": "string",
    "capacity_kw": "double",
    "proposed_capacity_kw": "double",
    "bess_power_kw": "double",
    "decision": "string",
    "binding_constraints": "array<string>",
    "requires_human_review": "boolean",
    "regulatory_override": "string",
    "worst_case_voltage_pu": "double",
    "min_scr_n1": "double",
    "max_thermal_loading_pct": "double",
    "feeder_model_sha256": "string",
    "reproducibility_sha256": "string",
    "queue_fingerprint": "string",
    "cache_hit": "boolean",
    "solver_convergence_verified": "boolean",
    "verdict_raw_json": "string",
    "year": "string",
    "month": "string",
    "day": "string",
}


def float_to_dynamodb_decimal(obj: Any) -> Any:
    """Recursively cast Python `float` values to `Decimal(str(x))` for DynamoDB `put_item`.

    Why `Decimal(str(obj))` is used instead of `Decimal(obj)`:
        Direct float-to-Decimal conversion captures IEEE-754 binary rounding artifacts
        (`Decimal(800.1)` -> `800.1000000000000227...`), whereas `str(obj)` preserves exact
        decimal physics figures (`Decimal("800.1")`).
    """
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, list):
        return [float_to_dynamodb_decimal(item) for item in obj]
    if isinstance(obj, tuple):
        return [float_to_dynamodb_decimal(item) for item in obj]
    if isinstance(obj, dict):
        return {k: float_to_dynamodb_decimal(v) for k, v in obj.items()}
    return obj


def to_dynamodb_audit_item(
    verdict: ScreeningVerdict,
    ttl_seconds: int = DEFAULT_AUDIT_TTL_SECONDS,
) -> dict[str, Any]:
    """Serialize a `ScreeningVerdict` into a float-free DynamoDB item with 7-day `expires_at_epoch` TTL."""
    worst_v = (
        verdict.tier2.peak_voltage_pu
        if verdict.tier2 is not None
        else verdict.tier1.predicted_peak_v_pu
    )
    raw = verdict.model_dump(mode="json")
    raw["pk_network_id"] = f"NETWORK#{verdict.connection_point.network_id}"
    raw["sk_timestamp_utc"] = verdict.audit_receipt.timestamp_utc
    raw["network_id"] = verdict.connection_point.network_id
    raw["timestamp_utc"] = verdict.audit_receipt.timestamp_utc
    raw["receipt_id"] = verdict.audit_receipt.receipt_id
    raw["capacity_kw"] = float(verdict.application.proposed_capacity_kw)
    raw["worst_case_voltage_pu"] = float(worst_v)
    raw["expires_at_epoch"] = int(time.time()) + max(60, int(ttl_seconds))
    return float_to_dynamodb_decimal(raw)


def flatten_verdict_for_parquet(verdict: ScreeningVerdict) -> dict[str, Any]:
    """Flatten `ScreeningVerdict` to top-level Parquet columns to avoid Hive nested-STRUCT drift."""
    ts = verdict.audit_receipt.timestamp_utc
    date_part = ts.split("T", 1)[0]
    parts = date_part.split("-")
    year, month, day = (parts[0], parts[1], parts[2]) if len(parts) == 3 else ("1970", "01", "01")

    worst_v = (
        verdict.tier2.peak_voltage_pu
        if verdict.tier2 is not None
        else verdict.tier1.predicted_peak_v_pu
    )
    max_loading = (
        max(verdict.tier2.max_line_loading_pct, verdict.tier2.transformer_loading_pct)
        if verdict.tier2 is not None
        else verdict.tier1.estimated_thermal_loading_pct
    )

    return {
        "pk_network_id": f"NETWORK#{verdict.connection_point.network_id}",
        "sk_timestamp_utc": ts,
        "timestamp_utc": ts,
        "receipt_id": verdict.audit_receipt.receipt_id,
        "application_id": verdict.application.application_id,
        "network_id": verdict.connection_point.network_id,
        "connection_node": verdict.connection_point.connection_node,
        "voltage_tier": verdict.connection_point.voltage_tier.value,
        "nominal_kv": float(verdict.connection_point.nominal_kv),
        "asset_type": verdict.application.asset_type.value,
        "capacity_kw": float(verdict.application.proposed_capacity_kw),
        "proposed_capacity_kw": float(verdict.application.proposed_capacity_kw),
        "bess_power_kw": float(verdict.application.bess_power_kw),
        "decision": verdict.decision.value,
        "binding_constraints": [c.value for c in verdict.binding_constraints],
        "requires_human_review": bool(verdict.requires_human_review),
        "regulatory_override": verdict.regulatory_override or "",
        "worst_case_voltage_pu": float(worst_v),
        "min_scr_n1": float(verdict.tier1.scr_n1),
        "max_thermal_loading_pct": float(max_loading),
        "feeder_model_sha256": verdict.audit_receipt.feeder_model_sha256,
        "reproducibility_sha256": verdict.audit_receipt.reproducibility_sha256,
        "queue_fingerprint": verdict.audit_receipt.queue_fingerprint,
        "cache_hit": bool(verdict.audit_receipt.cache_hit),
        "solver_convergence_verified": bool(verdict.audit_receipt.solver_convergence_verified),
        "verdict_raw_json": json.dumps(verdict.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
        "year": year,
        "month": month,
        "day": day,
    }


def infer_athena_column_types(row: dict[str, Any]) -> dict[str, str]:
    """Infer Athena/Glue SQL column types from a flattened audit row in pure Python."""
    inferred: dict[str, str] = {}
    for col, val in row.items():
        if isinstance(val, bool):
            inferred[col] = "boolean"
        elif isinstance(val, int):
            inferred[col] = "bigint"
        elif isinstance(val, float):
            inferred[col] = "double"
        elif isinstance(val, list):
            inferred[col] = "array<string>"
        else:
            inferred[col] = "string"
    return inferred

