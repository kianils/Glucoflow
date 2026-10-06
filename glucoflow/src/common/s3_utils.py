"""
s3_utils.py
Thin wrappers around boto3 S3 operations.

Design principles:
  - All uploads are idempotent: uploading the same key twice overwrites with the same data
  - All functions raise rather than swallow errors — let callers decide on retry logic
  - Keys always follow the pattern: source=<source>/date=<YYYY-MM-DD>/<filename>
    This makes Athena partition pruning free (no extra MSCK REPAIR TABLE needed)
"""

import json
import logging
from datetime import date
from pathlib import Path
from typing import Union

import boto3

logger = logging.getLogger(__name__)


def get_s3_client(region: str):
    return boto3.client("s3", region_name=region)


def build_key(source: str, filename: str, dt: date = None) -> str:
    """
    Build an S3 object key using Hive-style partitioning.

    Example:
        build_key("shanghai_t1dm", "1001_0.jsonl", date(2021, 7, 30))
        → "source=shanghai_t1dm/date=2021-07-30/1001_0.jsonl"
    """
    dt = dt or date.today()
    return f"source={source}/date={dt.isoformat()}/{filename}"


def upload_file(
    s3_client,
    local_path: Union[str, Path],
    bucket: str,
    key: str,
    content_type: str = "application/json",
) -> str:
    """Upload a local file to S3. Returns the full s3:// URI."""
    local_path = Path(local_path)
    logger.info(f"Uploading {local_path.name} → s3://{bucket}/{key}")
    s3_client.upload_file(
        Filename=str(local_path),
        Bucket=bucket,
        Key=key,
        ExtraArgs={"ContentType": content_type},
    )
    return f"s3://{bucket}/{key}"


def upload_bytes(s3_client, data: bytes, bucket: str, key: str, content_type: str = "application/json") -> str:
    """Upload raw bytes to S3. Returns the full s3:// URI."""
    logger.info(f"Uploading bytes → s3://{bucket}/{key}")
    s3_client.put_object(Body=data, Bucket=bucket, Key=key, ContentType=content_type)
    return f"s3://{bucket}/{key}"


def upload_jsonl(s3_client, records: list, bucket: str, key: str) -> str:
    """Serialize a list of dicts as newline-delimited JSON and upload."""
    body = "\n".join(json.dumps(r) for r in records).encode("utf-8")
    return upload_bytes(s3_client, body, bucket, key, content_type="application/x-ndjson")


def move_to_quarantine(s3_client, source_bucket: str, source_key: str, quarantine_bucket: str, reason: str):
    """Copy a Bronze object to quarantine with a reason tag, then delete from Bronze."""
    quarantine_key = f"reason={reason}/{source_key}"
    # Copy
    s3_client.copy_object(
        CopySource={"Bucket": source_bucket, "Key": source_key},
        Bucket=quarantine_bucket,
        Key=quarantine_key,
    )
    # Delete original
    s3_client.delete_object(Bucket=source_bucket, Key=source_key)
    logger.warning(f"Quarantined s3://{source_bucket}/{source_key} → reason={reason}")
