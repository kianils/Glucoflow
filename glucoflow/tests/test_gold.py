"""
test_gold.py — unit tests for src/transform/gold.py

No real AWS credentials required. All tests are mock-based.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.transform.gold import (
    GoldReading,
    calculate_gmi,
    aggregate_gold_metrics,
    write_gold_to_s3,
)


# ---------------------------------------------------------------------------
#  Clinical GMI Formulas
# ---------------------------------------------------------------------------

def test_calculate_gmi_normal():
    """calculate_gmi should compute correct GMI values for normal glucose averages."""
    # 100 mg/dL average -> 3.31 + (0.02392 * 100) = 5.702 -> 5.7
    assert calculate_gmi(100.0) == 5.7
    # 150 mg/dL average -> 3.31 + (0.02392 * 150) = 6.898 -> 6.9
    assert calculate_gmi(150.0) == 6.9


def test_calculate_gmi_zero_or_negative():
    """calculate_gmi should return None for zero or negative averages."""
    assert calculate_gmi(0.0) is None
    assert calculate_gmi(-50.0) is None


# ---------------------------------------------------------------------------
#  GoldReading Immutability & Validation
# ---------------------------------------------------------------------------

def test_gold_reading_instantiation():
    """GoldReading should instantiate successfully with valid parameters."""
    reading = GoldReading(
        patient_id="sim_adult#001",
        day="2023-01-01",
        tir_percent=85.5,
        mean_glucose=135.2,
        hypo_events=2,
        severe_hypo_events=0,
        cv_percent=17.4,
        gmi=6.55,
        source_system="simglucose",
    )
    assert reading.patient_id == "sim_adult#001"
    assert reading.day == "2023-01-01"
    assert reading.tir_percent == 85.5
    assert reading.gmi == 6.55
    assert reading.gold_schema_version == "2.0"  # v2: includes clinical profile fields
    assert isinstance(reading.gold_processed_at, datetime)


def test_gold_reading_immutability():
    """GoldReading must be immutable (frozen=True) to prevent downstream modifications."""
    reading = GoldReading(
        patient_id="sim_adult#001",
        day="2023-01-01",
        tir_percent=85.5,
        mean_glucose=135.2,
        hypo_events=2,
        severe_hypo_events=0,
        cv_percent=17.4,
        gmi=6.55,
        source_system="simglucose",
    )
    with pytest.raises((ValidationError, ValidationError)):
        # Pydantic v2 raises ValidationError when trying to set an attribute of a frozen model
        reading.patient_id = "new_id"


# ---------------------------------------------------------------------------
#  Aggregation & Extraction
# ---------------------------------------------------------------------------

def _make_silver_jsonl_lines(rows: list[dict]) -> bytes:
    """Helper: serialise Silver rows to JSONL bytes for mocking S3 get_object."""
    import json as _json
    return "\n".join(_json.dumps(r) for r in rows).encode()


def _mock_s3_for_silver(silver_rows: list[dict]):
    """
    Return a mock S3 client whose paginator yields one page with one object,
    and get_object returns the silver_rows serialised as JSONL.
    """
    mock_s3 = MagicMock()
    # paginator → one page → one object
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [
        {"Contents": [{"Key": "source=simglucose/date=2023-10-07/sim.jsonl"}]}
    ]
    mock_s3.get_paginator.return_value = mock_paginator
    # get_object → JSONL body
    mock_body = MagicMock()
    mock_body.read.return_value = _make_silver_jsonl_lines(silver_rows)
    mock_s3.get_object.return_value = {"Body": mock_body}
    return mock_s3


def test_aggregate_gold_metrics_success():
    """aggregate_gold_metrics should read Silver JSONL from S3 and compute metrics correctly."""
    # Two patients: adult (high glucose) and child (low glucose with hypos)
    silver_rows = [
        # adult: 5 readings, all in range 70–180, no hypos
        *[
            {"patient_id": "sim_adult#001", "event_time": "2023-10-07T00:00:00Z",
             "glucose_mgdl": 142.0, "clinical_range": "normal",
             "time_in_range_eligible": True, "source_system": "simglucose"}
            for _ in range(5)
        ],
        # child: 4 in range + 1 hypo reading (< 70)
        *[
            {"patient_id": "sim_child#001", "event_time": "2023-10-07T00:00:00Z",
             "glucose_mgdl": 115.0, "clinical_range": "normal",
             "time_in_range_eligible": True, "source_system": "simglucose"}
            for _ in range(4)
        ],
        {"patient_id": "sim_child#001", "event_time": "2023-10-07T01:00:00Z",
         "glucose_mgdl": 50.0, "clinical_range": "severe_hypo",
         "time_in_range_eligible": False, "source_system": "simglucose"},
    ]
    mock_s3 = _mock_s3_for_silver(silver_rows)
    readings = aggregate_gold_metrics(mock_s3, "test-silver-bucket")

    assert len(readings) == 2
    by_pid = {r.patient_id: r for r in readings}

    adult = by_pid["sim_adult#001"]
    assert adult.tir_percent == 100.0
    assert adult.mean_glucose == 142.0
    assert adult.hypo_events == 0
    assert adult.severe_hypo_events == 0
    assert adult.gmi == round(3.31 + (0.02392 * 142.0), 2)
    assert adult.source_system == "simglucose"

    child = by_pid["sim_child#001"]
    assert child.hypo_events == 1        # one reading < 70
    assert child.severe_hypo_events == 1  # 50 mg/dL < 54
    # The hypo row has tir_eligible=False so it is excluded from TIR denominator.
    # All 4 in-range rows ARE tir_eligible, so TIR = 4/4 = 100%.
    # This is clinically correct — TIR only counts eligible readings.
    assert child.tir_percent == 100.0


def test_aggregate_gold_metrics_skips_malformed_rows():
    """aggregate_gold_metrics should silently drop rows with non-numeric glucose."""
    silver_rows = [
        # valid row
        {"patient_id": "sim_adolescent#001", "event_time": "2023-10-07T00:00:00Z",
         "glucose_mgdl": 130.0, "clinical_range": "normal",
         "time_in_range_eligible": True, "source_system": "simglucose"},
        # malformed glucose — should be silently dropped by pd.to_numeric(errors='coerce')
        {"patient_id": "sim_child#001", "event_time": "2023-10-07T00:00:00Z",
         "glucose_mgdl": "INVALID_FLOAT", "clinical_range": "normal",
         "time_in_range_eligible": True, "source_system": "simglucose"},
    ]
    mock_s3 = _mock_s3_for_silver(silver_rows)
    readings = aggregate_gold_metrics(mock_s3, "test-silver-bucket")

    # Only the valid row should produce a GoldReading
    assert len(readings) == 1
    assert readings[0].patient_id == "sim_adolescent#001"


# ---------------------------------------------------------------------------
#  S3 Writing / Upload
# ---------------------------------------------------------------------------

@patch("src.transform.gold.upload_jsonl")
def test_write_gold_to_s3_success(mock_upload_jsonl):
    """write_gold_to_s3 should upload Gold readings to S3 with correct partition key structure."""
    gold_readings = [
        GoldReading(
            patient_id="sim_adult#001",
            day="2023-10-07",
            tir_percent=85.5,
            mean_glucose=135.2,
            hypo_events=2,
            severe_hypo_events=0,
            cv_percent=17.4,
            gmi=6.55,
            source_system="simglucose",
        ),
        GoldReading(
            patient_id="sim_child#001",
            day="2023-10-07",
            tir_percent=72.0,
            mean_glucose=122.1,
            hypo_events=4,
            severe_hypo_events=1,
            cv_percent=21.3,
            gmi=6.23,
            source_system="simglucose",
        )
    ]

    mock_s3 = MagicMock()
    count = write_gold_to_s3(mock_s3, gold_readings, "my-gold-bucket")

    assert count == 2
    
    # Verify mock uploads are correctly constructed
    assert mock_upload_jsonl.call_count == 2
    
    # First call checks
    first_call_args = mock_upload_jsonl.call_args_list[0]
    # args: (s3_client, records, bucket, key)
    assert first_call_args[0][2] == "my-gold-bucket"
    assert first_call_args[0][3] == "source=simglucose/date=2023-10-07/sim_adult#001_daily.jsonl"
    
    # Second call checks
    second_call_args = mock_upload_jsonl.call_args_list[1]
    assert second_call_args[0][3] == "source=simglucose/date=2023-10-07/sim_child#001_daily.jsonl"
