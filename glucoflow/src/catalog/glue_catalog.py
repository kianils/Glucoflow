"""
glue_catalog.py — GlucoFlow Glue/Athena catalog registration

Provides three functions for registering the Silver layer table in the
AWS Glue Data Catalog so it is queryable via Athena:

  ensure_database(db_name, client=None)
      Create the Glue database if it does not already exist.
      Swallows AlreadyExistsException so it is safe to call every deploy.

  table_exists(db_name, table_name, client=None) -> bool
      Return True if the table is registered in Glue, False otherwise.

  register_silver_table(db_name, table_name, s3_location, client=None)
      Idempotently register (or update) the Silver JSONL table in Glue.
      If the table already exists it is updated; otherwise it is created.

All functions accept an optional `client` parameter for dependency
injection / unit testing — callers pass a mock, prod code uses None
and a real boto3 glue client is created on first call.
"""

import logging
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def _get_client(client):
    """Return the provided client or create a real boto3 Glue client."""
    if client is not None:
        return client
    return boto3.client("glue")


# Silver table schema — matches SilverReading fields written as JSONL
_SILVER_COLUMNS = [
    {"Name": "patient_id",              "Type": "string"},
    {"Name": "lineage_id",              "Type": "string"},
    {"Name": "event_time",              "Type": "timestamp"},
    {"Name": "ingest_time",             "Type": "timestamp"},
    {"Name": "glucose_mgdl",            "Type": "double"},
    {"Name": "glucose_flag",            "Type": "string"},
    {"Name": "source_system",           "Type": "string"},
    {"Name": "batch_or_stream",         "Type": "string"},
    {"Name": "raw_payload",             "Type": "string"},
    {"Name": "clinical_range",          "Type": "string"},
    {"Name": "time_in_range_eligible",  "Type": "boolean"},
    {"Name": "hour_of_day",             "Type": "int"},
    {"Name": "day_of_week",             "Type": "string"},
    {"Name": "silver_schema_version",   "Type": "string"},
    {"Name": "silver_processed_at",     "Type": "timestamp"},
]


def ensure_database(db_name: str, client=None) -> None:
    """
    Create the Glue database ``db_name`` if it does not already exist.

    AlreadyExistsException is silently swallowed so this function is
    idempotent — safe to call on every deployment.

    Parameters
    ----------
    db_name : str
        Name of the Glue/Athena database to create.
    client : boto3 Glue client, optional
        Injected for testing.  A real client is created when None.
    """
    glue = _get_client(client)
    try:
        glue.create_database(
            DatabaseInput={
                "Name": db_name,
                "Description": "GlucoFlow Silver-layer Athena database",
            }
        )
        logger.info("Created Glue database: %s", db_name)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "AlreadyExistsException":
            logger.debug("Glue database already exists: %s", db_name)
        else:
            raise


def table_exists(db_name: str, table_name: str, client=None) -> bool:
    """
    Return True if *table_name* exists in *db_name*, False otherwise.

    Uses GetTable; an EntityNotFoundException means the table is absent.

    Parameters
    ----------
    db_name    : str  — Glue database name
    table_name : str  — Glue table name
    client     : boto3 Glue client, optional
    """
    glue = _get_client(client)
    try:
        glue.get_table(DatabaseName=db_name, Name=table_name)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "EntityNotFoundException":
            return False
        raise


def register_silver_table(
    db_name: str,
    table_name: str,
    s3_location: str,
    client=None,
) -> None:
    """
    Register (or update) the Silver JSONL table in the Glue Data Catalog.

    The table uses the OpenCSVSerde-equivalent for JSON (LazySimpleSerDe is
    not ideal for nested data; we use the Glue JSON SerDe).  Partitioned by
    ``source`` and ``date`` to align with the Hive-style S3 key layout
    written by s3_utils.build_key().

    Parameters
    ----------
    db_name      : str  — Glue database name
    table_name   : str  — Glue table name
    s3_location  : str  — s3:// URI for the Silver prefix, e.g.
                          "s3://my-silver-bucket/silver/"
    client       : boto3 Glue client, optional
    """
    glue = _get_client(client)

    table_input = {
        "Name": table_name,
        "Description": "GlucoFlow Silver-layer CGM readings",
        "StorageDescriptor": {
            "Columns": _SILVER_COLUMNS,
            "Location": s3_location,
            "InputFormat":  "org.apache.hadoop.mapred.TextInputFormat",
            "OutputFormat": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
            "SerdeInfo": {
                "SerializationLibrary": "org.openx.data.jsonserde.JsonSerDe",
                "Parameters": {"serialization.format": "1"},
            },
            "Compressed": False,
        },
        "PartitionKeys": [
            {"Name": "source", "Type": "string"},
            {"Name": "date",   "Type": "string"},
        ],
        "TableType": "EXTERNAL_TABLE",
        "Parameters": {
            "classification":         "json",
            "EXTERNAL":               "TRUE",
            "has_encrypted_data":     "false",
        },
    }

    if table_exists(db_name, table_name, client=glue):
        logger.info("Updating existing Glue table %s.%s", db_name, table_name)
        glue.update_table(DatabaseName=db_name, TableInput=table_input)
    else:
        logger.info("Creating Glue table %s.%s", db_name, table_name)
        glue.create_table(DatabaseName=db_name, TableInput=table_input)
