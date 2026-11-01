"""Reference SQS Dead-Letter Queue (DLQ) replay utility for DynamoDB Stream Parquet archival.

Fetches failed shard sequence envelopes from SQS, pulls the raw stream records via
`AT_SEQUENCE_NUMBER`, synchronously invokes the archiver Lambda, and clears the DLQ message.
"""

from __future__ import annotations

import json
import os

import boto3  # type: ignore[import-not-found]


def replay_single_dlq_batch() -> None:
    dlq_url = os.environ["GRID_AUDIT_DLQ_URL"]
    archiver_fn = os.environ.get("GRID_AUDIT_ARCHIVER_LAMBDA", "AuditArchiverLambda")

    sqs = boto3.client("sqs")
    ddb_streams = boto3.client("dynamodbstreams")
    lambda_client = boto3.client("lambda")

    resp = sqs.receive_message(QueueUrl=dlq_url, MaxNumberOfMessages=1, WaitTimeSeconds=2)
    messages = resp.get("Messages", [])
    if not messages:
        print("DLQ is empty.")
        return

    msg = messages[0]
    envelope = json.loads(msg["Body"])
    batch_info = envelope.get("DDBStreamBatchInfo")
    if not batch_info:
        print("Skipping non-DDBStreamBatchInfo message.")
        return

    shard_it = ddb_streams.get_shard_iterator(
        StreamArn=batch_info["streamArn"],
        ShardId=batch_info["shardId"],
        ShardIteratorType="AT_SEQUENCE_NUMBER",
        SequenceNumber=batch_info["startSequenceNumber"],
    )["ShardIterator"]

    records = ddb_streams.get_records(
        ShardIterator=shard_it,
        Limit=int(batch_info["batchSize"]),
    ).get("Records", [])

    invoke_resp = lambda_client.invoke(
        FunctionName=archiver_fn,
        InvocationType="RequestResponse",
        Payload=json.dumps({"Records": records}).encode("utf-8"),
    )

    if invoke_resp.get("StatusCode") == 200 and "FunctionError" not in invoke_resp:
        sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=msg["ReceiptHandle"])
        print(f"Replayed {len(records)} stream records and cleared DLQ entry.")
    else:
        print(f"Replay failed; message retained in DLQ: {invoke_resp.get('FunctionError')}")


if __name__ == "__main__":
    replay_single_dlq_batch()

