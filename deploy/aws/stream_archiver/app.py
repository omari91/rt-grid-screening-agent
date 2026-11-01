"""AWS Lambda DynamoDB Stream -> Immutable S3 Snappy Parquet + Glue Catalog Archiver.

Why this handler runs with the AWS-managed `AWSSDKPandas` layer and zero local bloat:
    1. TTL Ghost-Record Filtering: DynamoDB background TTL purges (`expires_at_epoch`)
       emit `REMOVE` events on the stream; filtering strictly to `('INSERT', 'MODIFY')`
       prevents archiving empty deletion tombstones into the 7-year WORM S3 bucket.
    2. Decimal Normalization for Athena Type Safety: DynamoDB stores all numbers as `N`
       (`Decimal`). Converting numeric fields cleanly to Python `float`/`int` ensures
       Pandas & `awswrangler` emit native Parquet `DOUBLE`/`BIGINT` columns rather than
       `OBJECT`/`STRING`, avoiding `HIVE_BAD_DATA` errors in downstream TSO SQL queries.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

try:
    from boto3.dynamodb.types import TypeDeserializer  # type: ignore[import-not-found]

    _BOTO_DESERIALIZER: Any = TypeDeserializer()
except ModuleNotFoundError:  # Offline CI / local unit-test fallback (no boto3 required in core venv)
    _BOTO_DESERIALIZER = None


def _stdlib_deserialize_attr(attr: dict[str, Any]) -> Any:
    """Zero-dependency DynamoDB AttributeValue deserializer matching `boto3.TypeDeserializer`."""
    if "S" in attr:
        return str(attr["S"])
    if "N" in attr:
        num_str = str(attr["N"])
        return Decimal(num_str)
    if "BOOL" in attr:
        return bool(attr["BOOL"])
    if "NULL" in attr:
        return None
    if "L" in attr:
        return [_stdlib_deserialize_attr(x) for x in attr["L"]]
    if "M" in attr:
        return {k: _stdlib_deserialize_attr(v) for k, v in attr["M"].items()}
    if "SS" in attr:
        return set(attr["SS"])
    if "NS" in attr:
        return {Decimal(str(x)) for x in attr["NS"]}
    return attr


def _normalize_decimals(val: Any, key_name: str = "") -> Any:
    """Convert `Decimal` instances into `int` (for epoch/count integers) or `float` (for physics metrics)."""
    if isinstance(val, Decimal):
        if key_name in ("expires_at_epoch",) or (val == val.to_integral_value() and "." not in str(val) and key_name.endswith(("_count", "_epoch", "_rank"))):
            return int(val)
        return float(val)
    if isinstance(val, list):
        return [_normalize_decimals(v) for v in val]
    if isinstance(val, dict):
        return {k: _normalize_decimals(v, k) for k, v in val.items()}
    return val


def unmarshal_dynamodb_image(raw_record: dict[str, Any]) -> dict[str, Any]:
    """Recursively convert a DynamoDB `NewImage` attribute map into standard Python primitives."""
    if _BOTO_DESERIALIZER is not None:
        deserialized = {k: _BOTO_DESERIALIZER.deserialize(v) for k, v in raw_record.items()}
    else:
        deserialized = {k: _stdlib_deserialize_attr(v) for k, v in raw_record.items()}
    return _normalize_decimals(deserialized)


def extract_partitioned_records(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Filter out TTL `REMOVE` tombstones, unmarshal DynamoDB images, and append `year`/`month`/`day`."""
    parsed_records: list[dict[str, Any]] = []
    for record in event.get("Records", []):
        if record.get("eventName") not in ("INSERT", "MODIFY"):
            continue

        raw_image = record.get("dynamodb", {}).get("NewImage")
        if not raw_image:
            continue

        item = unmarshal_dynamodb_image(raw_image)
        timestamp_str = str(item.get("timestamp_utc") or item.get("sk_timestamp_utc") or "").strip()
        if not timestamp_str:
            logger.warning("Skipping record missing timestamp_utc / sk_timestamp_utc.")
            continue

        try:
            dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            item["timestamp_utc"] = timestamp_str
            item["year"] = f"{dt.year:04d}"
            item["month"] = f"{dt.month:02d}"
            item["day"] = f"{dt.day:02d}"
        except ValueError:
            logger.warning("Skipping record due to malformed ISO-8601 timestamp: %s", timestamp_str)
            continue

        parsed_records.append(item)
    return parsed_records


def lambda_handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Archive `INSERT`/`MODIFY` DynamoDB Stream records to S3 Snappy Parquet and evolve Glue schema."""
    archive_bucket = os.environ["ARCHIVE_BUCKET"]
    glue_database = os.environ["GLUE_DATABASE"]
    glue_table = os.environ["GLUE_TABLE"]

    parsed_records = extract_partitioned_records(event)
    if not parsed_records:
        logger.info("No valid INSERT/MODIFY records to archive (likely a DynamoDB TTL REMOVE cycle).")
        return {"status": "skipped", "reason": "No actionable records", "archived_count": 0}

    import awswrangler as wr  # type: ignore[import-not-found]
    import pandas as pd  # type: ignore[import-not-found]

    df = pd.DataFrame(parsed_records)
    s3_path = f"s3://{archive_bucket}/receipts_parquet/"

    wr.s3.to_parquet(
        df=df,
        path=s3_path,
        dataset=True,
        database=glue_database,
        table=glue_table,
        partition_cols=["year", "month", "day"],
        mode="append",
        compression="snappy",
        schema_evolution=True,
        catalog_versioning=True,
    )

    logger.info("Successfully archived %d screening receipts to %s", len(df), s3_path)
    return {"status": "success", "archived_count": len(df)}

