"""
test_catalog.py — unit tests for src/catalog/glue_catalog.py

All tests use unittest.mock to avoid real AWS calls.
"""

import pytest
from unittest.mock import MagicMock, call
from botocore.exceptions import ClientError

from src.catalog.glue_catalog import (
    ensure_database,
    table_exists,
    register_silver_table,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_error(code: str) -> ClientError:
    """Build a ClientError with the given Glue error code."""
    return ClientError(
        {"Error": {"Code": code, "Message": "mocked"}},
        "MockedOperation",
    )


# ---------------------------------------------------------------------------
# table_exists
# ---------------------------------------------------------------------------

class TestTableExists:
    def test_returns_false_when_entity_not_found(self):
        """table_exists → False when Glue raises EntityNotFoundException."""
        mock_client = MagicMock()
        mock_client.get_table.side_effect = _client_error("EntityNotFoundException")

        result = table_exists("glucoflow_db", "silver", client=mock_client)

        assert result is False
        mock_client.get_table.assert_called_once_with(
            DatabaseName="glucoflow_db", Name="silver"
        )

    def test_returns_true_when_table_metadata_returned(self):
        """table_exists → True when Glue returns table metadata."""
        mock_client = MagicMock()
        mock_client.get_table.return_value = {
            "Table": {"Name": "silver", "DatabaseName": "glucoflow_db"}
        }

        result = table_exists("glucoflow_db", "silver", client=mock_client)

        assert result is True

    def test_reraises_unexpected_client_error(self):
        """table_exists re-raises ClientErrors that are not EntityNotFoundException."""
        mock_client = MagicMock()
        mock_client.get_table.side_effect = _client_error("AccessDeniedException")

        with pytest.raises(ClientError):
            table_exists("glucoflow_db", "silver", client=mock_client)


# ---------------------------------------------------------------------------
# register_silver_table
# ---------------------------------------------------------------------------

class TestRegisterSilverTable:
    def test_calls_create_table_with_correct_args_when_table_absent(self):
        """register_silver_table calls create_table with correct DatabaseName and TableName."""
        mock_client = MagicMock()
        # Simulate table not existing
        mock_client.get_table.side_effect = _client_error("EntityNotFoundException")

        register_silver_table(
            "glucoflow_db", "silver", "s3://my-silver/silver/", client=mock_client
        )

        mock_client.create_table.assert_called_once()
        kwargs = mock_client.create_table.call_args
        # Positional or keyword
        table_input = kwargs[1].get("TableInput") or kwargs[0][1]
        assert kwargs[1]["DatabaseName"] == "glucoflow_db"
        assert table_input["Name"] == "silver"

    def test_calls_update_table_when_table_already_exists(self):
        """register_silver_table calls update_table (not create_table) when table exists."""
        mock_client = MagicMock()
        mock_client.get_table.return_value = {
            "Table": {"Name": "silver", "DatabaseName": "glucoflow_db"}
        }

        register_silver_table(
            "glucoflow_db", "silver", "s3://my-silver/silver/", client=mock_client
        )

        mock_client.update_table.assert_called_once()
        mock_client.create_table.assert_not_called()

    def test_storage_descriptor_location_set_correctly(self):
        """StorageDescriptor.Location matches the supplied s3_location."""
        mock_client = MagicMock()
        mock_client.get_table.side_effect = _client_error("EntityNotFoundException")

        register_silver_table(
            "glucoflow_db", "silver", "s3://my-silver/silver/", client=mock_client
        )

        table_input = mock_client.create_table.call_args[1]["TableInput"]
        assert table_input["StorageDescriptor"]["Location"] == "s3://my-silver/silver/"


# ---------------------------------------------------------------------------
# ensure_database
# ---------------------------------------------------------------------------

class TestEnsureDatabase:
    def test_calls_create_database(self):
        """ensure_database calls create_database with the given name."""
        mock_client = MagicMock()

        ensure_database("glucoflow_db", client=mock_client)

        mock_client.create_database.assert_called_once()
        kwargs = mock_client.create_database.call_args[1]
        assert kwargs["DatabaseInput"]["Name"] == "glucoflow_db"

    def test_does_not_raise_when_database_already_exists(self):
        """ensure_database swallows AlreadyExistsException silently."""
        mock_client = MagicMock()
        mock_client.create_database.side_effect = _client_error(
            "AlreadyExistsException"
        )

        # Should not raise
        ensure_database("glucoflow_db", client=mock_client)

    def test_reraises_unexpected_error_from_create_database(self):
        """ensure_database re-raises non-AlreadyExists ClientErrors."""
        mock_client = MagicMock()
        mock_client.create_database.side_effect = _client_error("AccessDeniedException")

        with pytest.raises(ClientError):
            ensure_database("glucoflow_db", client=mock_client)
