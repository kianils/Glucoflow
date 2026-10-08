"""
test_silver.py — Tests for the Silver transform layer

Test coverage:
  - TestClinicalClassification : _classify_clinical_range — all 8 branches
  - TestTransformRecord        : transform_record — valid rows, quarantine routing, enrichment
  - TestSilverReading          : SilverReading model constraints (immutability, schema version)
  - TestProcessBronzeObject    : process_bronze_object — multi-row JSONL, malformed lines
  - TestTimeEnrichment         : hour_of_day + day_of_week extraction correctness
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.transform.silver import (
    SilverReading,
    _classify_clinical_range,
    transform_record,
    process_bronze_object,
    SEVERE_HYPO_THRESHOLD,
    HYPO_THRESHOLD,
    TIR_UPPER,
    HYPER_THRESHOLD,
)

# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #

def _make_bronze_record(**overrides) -> dict:
    """Build a minimal valid Bronze JSONL record dict."""
    base = {
        "patient_id":      "test_patient_001",
        "lineage_id":      str(uuid.uuid4()),
        "event_time":      "2023-01-01T08:30:00+00:00",
        "ingest_time":     "2023-01-01T09:00:00+00:00",
        "glucose_mgdl":    120.0,
        "glucose_flag":    "in_range",
        "source_system":   "simglucose",
        "batch_or_stream": "batch",
        "raw_payload":     {"original_col": "test_value"},
    }
    base.update(overrides)
    return base


def _make_s3_mock(jsonl_lines: list[dict]) -> MagicMock:
    """Build a mock S3 client whose get_object returns the given records as JSONL."""
    body_text = "\n".join(json.dumps(r) for r in jsonl_lines)
    mock_body  = MagicMock()
    mock_body.read.return_value = body_text.encode("utf-8")
    mock_s3    = MagicMock()
    mock_s3.get_object.return_value = {"Body": mock_body}
    return mock_s3


# ------------------------------------------------------------------ #
#  TestClinicalClassification                                          #
# ------------------------------------------------------------------ #

class TestClinicalClassification:
    """
    Tests for _classify_clinical_range.
    Every branch of the decision tree must be covered.
    """

    def test_low_signal_from_flag(self):
        """Sensor-reported 'low' → low_signal regardless of numeric value."""
        assert _classify_clinical_range(None, "low") == "low_signal"

    def test_high_signal_from_flag(self):
        """Sensor-reported 'high' → high_signal regardless of numeric value."""
        assert _classify_clinical_range(None, "high") == "high_signal"

    def test_no_signal_from_invalid_flag(self):
        assert _classify_clinical_range(None, "invalid") == "no_signal"

    def test_no_signal_when_glucose_is_none(self):
        """Even with a non-invalid flag, None glucose → no_signal."""
        assert _classify_clinical_range(None, "in_range") == "no_signal"

    def test_severe_hypo_below_threshold(self):
        """Glucose strictly below 54 mg/dL → severe_hypo."""
        assert _classify_clinical_range(50.0, "in_range") == "severe_hypo"
        assert _classify_clinical_range(53.9, "in_range") == "severe_hypo"

    def test_hypo_between_54_and_70(self):
        """54 ≤ glucose < 70 → hypo (ADA Level 1)."""
        assert _classify_clinical_range(SEVERE_HYPO_THRESHOLD, "in_range") == "hypo"
        assert _classify_clinical_range(65.0, "in_range") == "hypo"
        assert _classify_clinical_range(69.9, "in_range") == "hypo"

    def test_normal_time_in_range(self):
        """70 ≤ glucose ≤ 180 → normal (TIR zone)."""
        assert _classify_clinical_range(HYPO_THRESHOLD, "in_range") == "normal"
        assert _classify_clinical_range(100.0, "in_range") == "normal"
        assert _classify_clinical_range(TIR_UPPER, "in_range") == "normal"

    def test_hyper_between_180_and_250(self):
        """180 < glucose ≤ 250 → hyper (ADA Level 1)."""
        assert _classify_clinical_range(181.0, "in_range") == "hyper"
        assert _classify_clinical_range(HYPER_THRESHOLD, "in_range") == "hyper"

    def test_severe_hyper_above_250(self):
        """Glucose > 250 mg/dL → severe_hyper (ADA Level 2)."""
        assert _classify_clinical_range(251.0, "in_range") == "severe_hyper"
        assert _classify_clinical_range(350.0, "out_of_range") == "severe_hyper"
        assert _classify_clinical_range(400.0, "out_of_range") == "severe_hyper"

    def test_boundary_at_54_is_hypo_not_severe(self):
        """Exactly 54.0 is the threshold — should be hypo, not severe_hypo."""
        assert _classify_clinical_range(54.0, "in_range") == "hypo"

    def test_boundary_at_180_is_normal_not_hyper(self):
        """Exactly 180.0 is still within TIR — should be normal, not hyper."""
        assert _classify_clinical_range(180.0, "in_range") == "normal"


# ------------------------------------------------------------------ #
#  TestTransformRecord                                                 #
# ------------------------------------------------------------------ #

class TestTransformRecord:
    """
    Tests for transform_record — the per-row transform function.
    Covers valid paths, quarantine routing, and enrichment fields.
    """

    def test_valid_in_range_produces_silver_reading(self):
        record = _make_bronze_record(glucose_mgdl=120.0, glucose_flag="in_range")
        result = transform_record(record)
        assert result is not None
        assert isinstance(result, SilverReading)

    def test_invalid_flag_returns_none(self):
        """Records with glucose_flag='invalid' must be routed to quarantine (None return)."""
        record = _make_bronze_record(glucose_mgdl=None, glucose_flag="invalid")
        assert transform_record(record) is None

    def test_clinical_range_assigned_correctly(self):
        result = transform_record(_make_bronze_record(glucose_mgdl=120.0, glucose_flag="in_range"))
        assert result.clinical_range == "normal"

    def test_severe_hypo_classified_correctly(self):
        result = transform_record(_make_bronze_record(glucose_mgdl=50.0, glucose_flag="in_range"))
        assert result.clinical_range == "severe_hypo"

    def test_low_flag_routes_to_silver_as_low_signal(self):
        """'low' flag readings are NOT quarantined — they are valid, just low_signal."""
        record = _make_bronze_record(glucose_mgdl=None, glucose_flag="low")
        result = transform_record(record)
        assert result is not None
        assert result.clinical_range == "low_signal"

    def test_high_flag_routes_to_silver_as_high_signal(self):
        record = _make_bronze_record(glucose_mgdl=None, glucose_flag="high")
        result = transform_record(record)
        assert result is not None
        assert result.clinical_range == "high_signal"

    def test_tir_eligible_for_in_range(self):
        result = transform_record(_make_bronze_record(glucose_mgdl=100.0, glucose_flag="in_range"))
        assert result.time_in_range_eligible is True

    def test_tir_not_eligible_for_low_flag(self):
        result = transform_record(_make_bronze_record(glucose_mgdl=None, glucose_flag="low"))
        assert result.time_in_range_eligible is False

    def test_tir_not_eligible_for_high_flag(self):
        result = transform_record(_make_bronze_record(glucose_mgdl=None, glucose_flag="high"))
        assert result.time_in_range_eligible is False

    def test_tir_eligible_for_out_of_range(self):
        """out_of_range has a numeric value — include in TIR denominator."""
        result = transform_record(_make_bronze_record(glucose_mgdl=401.0, glucose_flag="out_of_range"))
        assert result.time_in_range_eligible is True

    def test_bronze_fields_preserved_verbatim(self):
        """Silver must carry every Bronze field unchanged."""
        record = _make_bronze_record()
        result = transform_record(record)
        assert result.patient_id      == record["patient_id"]
        assert result.lineage_id      == record["lineage_id"]
        assert result.source_system   == record["source_system"]
        assert result.batch_or_stream == record["batch_or_stream"]
        assert result.raw_payload     == record["raw_payload"]

    def test_silver_schema_version_is_set(self):
        result = transform_record(_make_bronze_record())
        assert result.silver_schema_version == "1.0"

    def test_silver_processed_at_is_utc_aware(self):
        result = transform_record(_make_bronze_record())
        assert result.silver_processed_at.tzinfo is not None

    def test_missing_required_field_raises(self):
        record = _make_bronze_record()
        del record["event_time"]
        with pytest.raises(KeyError):
            transform_record(record)

    def test_immutability_frozen(self):
        """SilverReading must be frozen — direct attribute assignment must raise."""
        from pydantic import ValidationError
        result = transform_record(_make_bronze_record())
        with pytest.raises((ValidationError, TypeError)):
            result.glucose_mgdl = 999.0


# ------------------------------------------------------------------ #
#  TestSilverReading                                                   #
# ------------------------------------------------------------------ #

class TestSilverReading:
    """
    Tests for the SilverReading Pydantic model itself.
    Covers validation constraints and field defaults.
    """

    def _base_kwargs(self) -> dict:
        return {
            "patient_id":             "patient_001",
            "lineage_id":             str(uuid.uuid4()),
            "event_time":             datetime(2023, 1, 1, 8, 0, tzinfo=timezone.utc),
            "ingest_time":            datetime(2023, 1, 1, 9, 0, tzinfo=timezone.utc),
            "glucose_mgdl":           120.0,
            "glucose_flag":           "in_range",
            "source_system":          "simglucose",
            "batch_or_stream":        "batch",
            "raw_payload":            {},
            "clinical_range":         "normal",
            "time_in_range_eligible": True,
            "hour_of_day":            8,
            "day_of_week":            "Sunday",
        }

    def test_valid_model_instantiates(self):
        reading = SilverReading(**self._base_kwargs())
        assert reading.patient_id == "patient_001"

    def test_hour_of_day_bounds_upper(self):
        from pydantic import ValidationError
        kwargs = self._base_kwargs()
        kwargs["hour_of_day"] = 24          # invalid: must be ≤ 23
        with pytest.raises(ValidationError):
            SilverReading(**kwargs)

    def test_hour_of_day_bounds_lower(self):
        from pydantic import ValidationError
        kwargs = self._base_kwargs()
        kwargs["hour_of_day"] = -1          # invalid: must be ≥ 0
        with pytest.raises(ValidationError):
            SilverReading(**kwargs)

    def test_schema_version_default(self):
        reading = SilverReading(**self._base_kwargs())
        assert reading.silver_schema_version == "1.0"

    def test_processed_at_defaults_to_now(self):
        before = datetime.now(timezone.utc)
        reading = SilverReading(**self._base_kwargs())
        after  = datetime.now(timezone.utc)
        assert before <= reading.silver_processed_at <= after


# ------------------------------------------------------------------ #
#  TestProcessBronzeObject                                             #
# ------------------------------------------------------------------ #

class TestProcessBronzeObject:
    """
    Tests for process_bronze_object — the S3 read + JSONL iteration layer.
    Uses mock S3 clients so no real AWS calls are made.
    """

    def test_valid_records_go_to_silver(self):
        records = [_make_bronze_record(glucose_mgdl=100.0, glucose_flag="in_range")]
        mock_s3 = _make_s3_mock(records)
        silver, quarantine = process_bronze_object(mock_s3, "test-bronze", "test/key.jsonl")
        assert len(silver) == 1
        assert len(quarantine) == 0

    def test_invalid_records_go_to_quarantine(self):
        records = [_make_bronze_record(glucose_mgdl=None, glucose_flag="invalid")]
        mock_s3 = _make_s3_mock(records)
        silver, quarantine = process_bronze_object(mock_s3, "test-bronze", "test/key.jsonl")
        assert len(silver) == 0
        assert len(quarantine) == 1

    def test_quarantine_reason_field_present(self):
        records = [_make_bronze_record(glucose_mgdl=None, glucose_flag="invalid")]
        mock_s3 = _make_s3_mock(records)
        _, quarantine = process_bronze_object(mock_s3, "test-bronze", "test/key.jsonl")
        assert "quarantine_reason" in quarantine[0]
        assert quarantine[0]["quarantine_reason"] == "glucose_flag_invalid"

    def test_mixed_valid_and_invalid(self):
        records = [
            _make_bronze_record(glucose_mgdl=120.0, glucose_flag="in_range"),
            _make_bronze_record(glucose_mgdl=None,  glucose_flag="invalid"),
            _make_bronze_record(glucose_mgdl=65.0,  glucose_flag="in_range"),
        ]
        mock_s3 = _make_s3_mock(records)
        silver, quarantine = process_bronze_object(mock_s3, "test-bronze", "test/key.jsonl")
        assert len(silver)     == 2
        assert len(quarantine) == 1

    def test_malformed_json_goes_to_quarantine(self):
        """A JSONL line that is not valid JSON should be quarantined, not crash the pipeline."""
        body_text = '{"glucose_mgdl": 100}\nnot valid json at all\n{"glucose_mgdl": 80}'
        mock_body = MagicMock()
        mock_body.read.return_value = body_text.encode("utf-8")
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": mock_body}

        # We can't fully process these without all required Bronze fields,
        # but malformed JSON lines should specifically not raise
        # (they'll become quarantine records with json_parse_error reason)
        silver, quarantine = process_bronze_object(mock_s3, "test-bronze", "test/key.jsonl")
        # The malformed line must appear in quarantine
        quarantine_reasons = [q.get("quarantine_reason", "") for q in quarantine]
        assert any("json_parse_error" in r for r in quarantine_reasons)

    def test_empty_lines_are_skipped(self):
        """Empty lines in JSONL are valid (common at end of file) — should be silently skipped."""
        body_text = "\n".join([
            json.dumps(_make_bronze_record(glucose_mgdl=110.0, glucose_flag="in_range")),
            "",  # blank line
            "",  # blank line
        ])
        mock_body = MagicMock()
        mock_body.read.return_value = body_text.encode("utf-8")
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": mock_body}

        silver, quarantine = process_bronze_object(mock_s3, "test-bronze", "test/key.jsonl")
        assert len(silver) == 1
        assert len(quarantine) == 0

    def test_all_low_and_high_records_reach_silver(self):
        """low + high flag records are valid readings — should NOT go to quarantine."""
        records = [
            _make_bronze_record(glucose_mgdl=None, glucose_flag="low"),
            _make_bronze_record(glucose_mgdl=None, glucose_flag="high"),
        ]
        mock_s3 = _make_s3_mock(records)
        silver, quarantine = process_bronze_object(mock_s3, "test-bronze", "test/key.jsonl")
        assert len(silver)     == 2
        assert len(quarantine) == 0


# ------------------------------------------------------------------ #
#  TestTimeEnrichment                                                  #
# ------------------------------------------------------------------ #

class TestTimeEnrichment:
    """
    Tests for hour_of_day and day_of_week enrichment.
    These are simple derivations but must be correct — Gold TIR by time-of-day
    queries depend entirely on them.
    """

    def test_hour_of_day_extracted_correctly(self):
        record = _make_bronze_record(event_time="2023-01-01T14:30:00+00:00")
        result = transform_record(record)
        assert result.hour_of_day == 14

    def test_midnight_is_hour_zero(self):
        record = _make_bronze_record(event_time="2023-01-01T00:00:00+00:00")
        result = transform_record(record)
        assert result.hour_of_day == 0

    def test_end_of_day_is_hour_23(self):
        record = _make_bronze_record(event_time="2023-01-01T23:59:00+00:00")
        result = transform_record(record)
        assert result.hour_of_day == 23

    def test_day_of_week_is_correct_string(self):
        # 2023-01-01 is a Sunday
        record = _make_bronze_record(event_time="2023-01-01T08:00:00+00:00")
        result = transform_record(record)
        assert result.day_of_week == "Sunday"

    def test_monday_is_labelled_monday(self):
        # 2023-01-02 is a Monday
        record = _make_bronze_record(event_time="2023-01-02T08:00:00+00:00")
        result = transform_record(record)
        assert result.day_of_week == "Monday"

    def test_naive_timestamp_is_treated_as_utc(self):
        """Bronze records without timezone info should be assumed UTC — not rejected."""
        record = _make_bronze_record(event_time="2023-01-15T10:00:00")  # no +00:00
        result = transform_record(record)
        assert result is not None
        assert result.hour_of_day == 10
