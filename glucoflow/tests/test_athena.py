"""
test_athena.py — unit tests for src/query/athena_runner.py

All tests mock boto3 Athena calls; no real AWS credentials required.
"""

import pytest
from unittest.mock import MagicMock, patch, call

from src.query.athena_runner import run_query


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(state: str = "SUCCEEDED", reason: str = ""):
    """
    Return a mock Athena client that simulates a query reaching *state*.

    start_query_execution returns execution id "query-123".
    get_query_execution returns *state* on first poll.
    get_query_results returns two data rows with columns a, b.
    """
    mock_client = MagicMock()

    mock_client.start_query_execution.return_value = {
        "QueryExecutionId": "query-123"
    }

    status_payload = {
        "QueryExecution": {
            "QueryExecutionId": "query-123",
            "Status": {"State": state, "StateChangeReason": reason},
        }
    }
    mock_client.get_query_execution.return_value = status_payload

    mock_client.get_query_results.return_value = {
        "ResultSet": {
            "Rows": [
                {"Data": [{"VarCharValue": "a"}, {"VarCharValue": "b"}]},
                {"Data": [{"VarCharValue": "1"}, {"VarCharValue": "2"}]},
                {"Data": [{"VarCharValue": "3"}, {"VarCharValue": "4"}]},
            ]
        }
    }

    return mock_client


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestRunQuerySucceeded:
    def test_returns_list_of_dicts(self):
        """run_query returns a list of dicts keyed by column names."""
        mock_client = _make_client("SUCCEEDED")

        with patch("src.query.athena_runner.time.sleep"):
            result = run_query(
                "SELECT 1", "glucoflow_db", "my-results", client=mock_client
            )

        assert result == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]

    def test_start_query_execution_called_with_correct_args(self):
        """run_query passes sql, db and output bucket to start_query_execution."""
        mock_client = _make_client("SUCCEEDED")

        with patch("src.query.athena_runner.time.sleep"):
            run_query("SELECT 1", "glucoflow_db", "my-results", client=mock_client)

        mock_client.start_query_execution.assert_called_once_with(
            QueryString="SELECT 1",
            QueryExecutionContext={"Database": "glucoflow_db"},
            ResultConfiguration={"OutputLocation": "s3://my-results/athena-results/"},
        )

    def test_get_query_execution_polled_with_execution_id(self):
        """run_query polls get_query_execution with the correct execution id."""
        mock_client = _make_client("SUCCEEDED")

        with patch("src.query.athena_runner.time.sleep"):
            run_query("SELECT 1", "glucoflow_db", "my-results", client=mock_client)

        mock_client.get_query_execution.assert_called_with(
            QueryExecutionId="query-123"
        )

    def test_empty_result_set_returns_empty_list(self):
        """run_query returns an empty list when Athena returns no rows."""
        mock_client = _make_client("SUCCEEDED")
        mock_client.get_query_results.return_value = {"ResultSet": {"Rows": []}}

        with patch("src.query.athena_runner.time.sleep"):
            result = run_query("SELECT 1", "glucoflow_db", "my-results", client=mock_client)

        assert result == []


# ---------------------------------------------------------------------------
# Failure / timeout paths
# ---------------------------------------------------------------------------

class TestRunQueryFailures:
    def test_raises_runtime_error_on_failed_state(self):
        """run_query raises RuntimeError when Athena state is FAILED."""
        mock_client = _make_client("FAILED", reason="Syntax error")

        with patch("src.query.athena_runner.time.sleep"):
            with pytest.raises(RuntimeError, match="FAILED"):
                run_query("BAD SQL", "glucoflow_db", "my-results", client=mock_client)

    def test_raises_runtime_error_on_cancelled_state(self):
        """run_query raises RuntimeError when Athena state is CANCELLED."""
        mock_client = _make_client("CANCELLED")

        with patch("src.query.athena_runner.time.sleep"):
            with pytest.raises(RuntimeError, match="CANCELLED"):
                run_query("SELECT 1", "glucoflow_db", "my-results", client=mock_client)

    def test_raises_timeout_error_when_query_never_completes(self):
        """run_query raises TimeoutError when max wait is exceeded."""
        mock_client = MagicMock()
        mock_client.start_query_execution.return_value = {
            "QueryExecutionId": "query-123"
        }
        # Always returns RUNNING — never reaches a terminal state
        mock_client.get_query_execution.return_value = {
            "QueryExecution": {
                "QueryExecutionId": "query-123",
                "Status": {"State": "RUNNING", "StateChangeReason": ""},
            }
        }

        # Patch both sleep AND _MAX_WAIT_S so the loop terminates quickly
        with patch("src.query.athena_runner.time.sleep"), \
             patch("src.query.athena_runner._MAX_WAIT_S", 4), \
             patch("src.query.athena_runner._POLL_INTERVAL_S", 2):
            with pytest.raises(TimeoutError, match="did not complete"):
                run_query("SELECT 1", "glucoflow_db", "my-results", client=mock_client)

    def test_error_message_includes_state_change_reason(self):
        """RuntimeError message includes the StateChangeReason from Athena."""
        mock_client = _make_client("FAILED", reason="Table not found")

        with patch("src.query.athena_runner.time.sleep"):
            with pytest.raises(RuntimeError, match="Table not found"):
                run_query("SELECT 1", "glucoflow_db", "my-results", client=mock_client)
