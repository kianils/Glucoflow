"""
test_schema.py — Tests for CanonicalReading schema (Prompt 2)

Coverage:
  - Normal numeric value (in_range)
  - "Low" string sentinel → flag="low", glucose_mgdl=None
  - "High" string sentinel → flag="high", glucose_mgdl=None
  - Out-of-range numeric (e.g. 500) → flag="out_of_range", value preserved
  - Garbage / non-numeric string → flag="invalid", glucose_mgdl=None
  - None / blank → flag="invalid"
  - Shanghai "/" null → flag="invalid"
  - from_raw() auto-sets ingest_time and lineage_id
  - from_raw() makes timezone-naive event_time UTC-aware
  - Immutability (frozen=True)
"""

import pytest
from datetime import datetime, timezone

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.schema import CanonicalReading, _classify_glucose

# ── Fixtures ────────────────────────────────────────────────────────────────

NOW_NAIVE = datetime(2023, 6, 1, 12, 0, 0)
NOW_UTC   = datetime(2023, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

BASE_ROW = {"raw_col": "test"}

def make(glucose_raw, source="simglucose", mode="batch"):
    return CanonicalReading.from_raw(
        row=BASE_ROW,
        patient_id="test_001",
        event_time=NOW_UTC,
        source_system=source,
        batch_or_stream=mode,
        glucose_raw=glucose_raw,
    )


# ── _classify_glucose unit tests ─────────────────────────────────────────────

class TestClassifyGlucose:
    def test_normal_value_in_range(self):
        val, flag = _classify_glucose(140.0)
        assert val == 140.0
        assert flag == "in_range"

    def test_boundary_low_is_in_range(self):
        val, flag = _classify_glucose(40.0)
        assert val == 40.0
        assert flag == "in_range"

    def test_boundary_high_is_in_range(self):
        val, flag = _classify_glucose(400.0)
        assert val == 400.0
        assert flag == "in_range"

    def test_numeric_above_400_is_out_of_range(self):
        val, flag = _classify_glucose(500.0)
        assert val == 500.0
        assert flag == "out_of_range"

    def test_numeric_below_40_is_out_of_range(self):
        val, flag = _classify_glucose(10.0)
        assert val == 10.0
        assert flag == "out_of_range"

    def test_string_Low(self):
        val, flag = _classify_glucose("Low")
        assert val is None
        assert flag == "low"

    def test_string_High(self):
        val, flag = _classify_glucose("High")
        assert val is None
        assert flag == "high"

    def test_case_insensitive_low(self):
        _, flag = _classify_glucose("low")
        assert flag == "low"

    def test_case_insensitive_high(self):
        _, flag = _classify_glucose("HIGH")
        assert flag == "high"

    def test_garbage_string(self):
        val, flag = _classify_glucose("not_a_number")
        assert val is None
        assert flag == "invalid"

    def test_none_input(self):
        val, flag = _classify_glucose(None)
        assert val is None
        assert flag == "invalid"

    def test_empty_string(self):
        val, flag = _classify_glucose("")
        assert val is None
        assert flag == "invalid"

    def test_slash_null_shanghai(self):
        """Shanghai uses '/' as a null sentinel."""
        val, flag = _classify_glucose("/")
        assert val is None
        assert flag == "invalid"

    def test_numeric_string_in_range(self):
        val, flag = _classify_glucose("142.5")
        assert val == 142.5
        assert flag == "in_range"


# ── CanonicalReading.from_raw integration tests ──────────────────────────────

class TestFromRaw:
    def test_normal_value(self):
        r = make(140.0)
        assert r.glucose_mgdl == 140.0
        assert r.glucose_flag == "in_range"
        assert r.patient_id == "test_001"
        assert r.source_system == "simglucose"
        assert r.batch_or_stream == "batch"

    def test_low_string(self):
        r = make("Low")
        assert r.glucose_mgdl is None
        assert r.glucose_flag == "low"

    def test_high_string(self):
        r = make("High")
        assert r.glucose_mgdl is None
        assert r.glucose_flag == "high"

    def test_out_of_range_numeric(self):
        r = make(500.0)
        assert r.glucose_mgdl == 500.0
        assert r.glucose_flag == "out_of_range"

    def test_garbage_string(self):
        r = make("???")
        assert r.glucose_mgdl is None
        assert r.glucose_flag == "invalid"

    def test_none_glucose(self):
        r = make(None)
        assert r.glucose_mgdl is None
        assert r.glucose_flag == "invalid"

    def test_ingest_time_auto_set(self):
        r = make(100.0)
        assert r.ingest_time is not None
        assert r.ingest_time.tzinfo is not None  # must be timezone-aware

    def test_lineage_id_is_uuid(self):
        import re
        r = make(100.0)
        uuid_pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        assert re.match(uuid_pattern, r.lineage_id), f"Not a UUID4: {r.lineage_id}"

    def test_each_call_gets_unique_lineage_id(self):
        r1 = make(100.0)
        r2 = make(100.0)
        assert r1.lineage_id != r2.lineage_id

    def test_raw_payload_preserved(self):
        r = make(120.0)
        assert r.raw_payload == BASE_ROW

    def test_naive_event_time_becomes_utc(self):
        r = CanonicalReading.from_raw(
            row={},
            patient_id="x",
            event_time=NOW_NAIVE,
            source_system="shanghai",
            batch_or_stream="batch",
            glucose_raw=100.0,
        )
        assert r.event_time.tzinfo == timezone.utc

    def test_immutable(self):
        r = make(100.0)
        with pytest.raises(Exception):
            r.glucose_mgdl = 999.0

    def test_dexcom_messy_source(self):
        r = make("Low", source="dexcom_messy")
        assert r.source_system == "dexcom_messy"

    def test_stream_mode(self):
        r = make(75.0, mode="stream")
        assert r.batch_or_stream == "stream"
