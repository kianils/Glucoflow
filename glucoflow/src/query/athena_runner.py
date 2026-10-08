"""
athena_runner.py — GlucoFlow Athena query runner

Single public function:

  run_query(sql, db, output_bucket, client=None) -> list[dict]
      Submit a SQL query to Athena, poll until completion, return rows.

Polling behaviour
-----------------
- Polls every 2 seconds.
- Raises TimeoutError if query has not completed within 60 seconds.
- Raises RuntimeError on FAILED / CANCELLED states.

Results are parsed from Athena's GetQueryResults response and returned
as a list of plain dicts keyed by column name (header row is stripped).

The optional `client` parameter accepts an injected boto3 Athena client,
allowing full unit-test coverage without real AWS calls.
"""

import logging
import time
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 2        # seconds between GetQueryExecution calls
_MAX_WAIT_S      = 60       # hard upper bound
_TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED"}


def _get_client(client):
    """Return the provided client or create a real boto3 Athena client."""
    if client is not None:
        return client
    return boto3.client("athena")


def run_query(
    sql: str,
    db: str,
    output_bucket: str,
    client=None,
) -> list[dict]:
    """
    Run *sql* against Athena database *db* and return results as dicts.

    Parameters
    ----------
    sql           : str  — Valid SQL statement (SELECT, SHOW, …)
    db            : str  — Glue/Athena database name
    output_bucket : str  — S3 bucket (no prefix) where Athena writes
                          query results, e.g. ``"my-athena-results"``
    client        : boto3 Athena client, optional — injected for testing

    Returns
    -------
    list[dict]
        Each dict maps column name → string value for one result row.
        Empty list for DDL/DML statements with no rows.

    Raises
    ------
    RuntimeError
        Query reached FAILED or CANCELLED state.
    TimeoutError
        Query did not complete within 60 seconds.
    """
    athena = _get_client(client)

    # --- 1. Start the query ---
    output_location = f"s3://{output_bucket}/athena-results/"
    response = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": db},
        ResultConfiguration={"OutputLocation": output_location},
    )
    execution_id = response["QueryExecutionId"]
    logger.info("Athena query started: %s", execution_id)

    # --- 2. Poll until terminal state ---
    elapsed = 0.0
    state = None
    while elapsed < _MAX_WAIT_S:
        time.sleep(_POLL_INTERVAL_S)
        elapsed += _POLL_INTERVAL_S

        status_response = athena.get_query_execution(
            QueryExecutionId=execution_id
        )
        state = (
            status_response["QueryExecution"]["Status"]["State"]
        )
        logger.debug("Query %s state: %s (%.0fs elapsed)", execution_id, state, elapsed)

        if state in _TERMINAL_STATES:
            break

    if state not in _TERMINAL_STATES:
        raise TimeoutError(
            f"Athena query {execution_id} did not complete within "
            f"{_MAX_WAIT_S}s (last state: {state})"
        )

    if state != "SUCCEEDED":
        reason = (
            status_response["QueryExecution"]["Status"]
            .get("StateChangeReason", "no reason provided")
        )
        raise RuntimeError(
            f"Athena query {execution_id} ended with state {state}: {reason}"
        )

    # --- 3. Fetch and parse results ---
    results_response = athena.get_query_results(
        QueryExecutionId=execution_id
    )
    return _parse_results(results_response)


def _parse_results(results_response: dict) -> list[dict]:
    """
    Convert Athena GetQueryResults response into a list of plain dicts.

    The first row of ResultSet.Rows is the column-header row — it is used
    as keys and then discarded from the output.
    """
    rows = results_response.get("ResultSet", {}).get("Rows", [])
    if not rows:
        return []

    # Header row contains column names
    headers = [col.get("VarCharValue", "") for col in rows[0]["Data"]]

    result = []
    for row in rows[1:]:          # skip header
        cells = [cell.get("VarCharValue", "") for cell in row["Data"]]
        result.append(dict(zip(headers, cells)))

    return result
