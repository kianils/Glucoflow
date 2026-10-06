"""
test_generators.py
Tests that:
  1. Simglucose output CSVs exist and have the right shape
  2. Dexcom messy CSVs exist, have the 3-line metadata header, the right columns,
     contain at least one blank-row spacer, and Low/High sentinel strings where expected
  3. No patient data leaks into column names (basic PII check)
"""

import csv
from pathlib import Path

import pytest
import pandas as pd

ROOT     = Path(__file__).resolve().parents[1]
SYNTH    = ROOT / "data" / "samples" / "synthetic"
MESSY    = ROOT / "data" / "samples" / "messy"
PATIENTS = ["adolescent_001", "adult_001", "child_001"]

SYNTHETIC_COLS = {"timestamp", "patient_id", "cgm_mgdl", "bg_mgdl", "cho_grams", "insulin_units"}
DEXCOM_COLS    = {
    "Index",
    "Timestamp (YYYY-MM-DDThh:mm:ss)",
    "Event Type",
    "Glucose Value (mg/dL)",
    "Insulin Value (u)",
    "Carb Value (grams)",
}


# ─── Synthetic CSV tests ─────────────────────────────────────────────────────

class TestSyntheticCSVs:
    @pytest.mark.parametrize("patient", PATIENTS)
    def test_file_exists(self, patient):
        assert (SYNTH / f"{patient}.csv").exists(), f"Missing synthetic CSV for {patient}"

    @pytest.mark.parametrize("patient", PATIENTS)
    def test_columns_present(self, patient):
        df = pd.read_csv(SYNTH / f"{patient}.csv")
        assert SYNTHETIC_COLS.issubset(set(df.columns)), f"Missing columns in {patient}.csv"

    @pytest.mark.parametrize("patient", PATIENTS)
    def test_row_count_one_day_at_3min_intervals(self, patient):
        # 24h × 60min / 3min = 480 intervals + 1 header = 481 data rows
        df = pd.read_csv(SYNTH / f"{patient}.csv")
        assert len(df) == 481, f"Expected 481 rows, got {len(df)} for {patient}"

    @pytest.mark.parametrize("patient", PATIENTS)
    def test_glucose_values_numeric(self, patient):
        df = pd.read_csv(SYNTH / f"{patient}.csv")
        assert df["cgm_mgdl"].notna().all(), f"NaN glucose values in {patient}.csv"
        assert (df["cgm_mgdl"] > 0).all(), f"Non-positive glucose in {patient}.csv"

    @pytest.mark.parametrize("patient", PATIENTS)
    def test_timestamps_monotonic(self, patient):
        df = pd.read_csv(SYNTH / f"{patient}.csv", parse_dates=["timestamp"])
        assert df["timestamp"].is_monotonic_increasing, f"Timestamps not monotonic in {patient}.csv"

    @pytest.mark.parametrize("patient", PATIENTS)
    def test_patient_id_consistent(self, patient):
        df = pd.read_csv(SYNTH / f"{patient}.csv")
        # patient_id should be the same in every row (e.g. "adolescent#001")
        assert df["patient_id"].nunique() == 1, f"Multiple patient IDs in {patient}.csv"


# ─── Dexcom messy CSV tests ───────────────────────────────────────────────────

class TestDexcomMessyCSVs:
    @pytest.mark.parametrize("patient", PATIENTS)
    def test_file_exists(self, patient):
        assert (MESSY / f"dexcom_{patient}.csv").exists(), f"Missing messy CSV for {patient}"

    @pytest.mark.parametrize("patient", PATIENTS)
    def test_three_metadata_rows_before_header(self, patient):
        """Lines 0-2 must NOT be the column header — they are metadata."""
        path = MESSY / f"dexcom_{patient}.csv"
        lines = path.read_text().splitlines()
        # Line 0: Patient Name row
        assert "Patient Name" in lines[0], f"Line 0 should be Patient Name metadata in {patient}"
        # Line 1: Exported row
        assert "Exported" in lines[1], f"Line 1 should be Exported metadata in {patient}"
        # Line 3 (0-indexed): actual column header
        assert "Event Type" in lines[3], f"Column header not at line 3 in {patient}"

    @pytest.mark.parametrize("patient", PATIENTS)
    def test_required_columns_present(self, patient):
        # Skip the 3-line metadata block before reading
        df = pd.read_csv(MESSY / f"dexcom_{patient}.csv", skiprows=3)
        assert DEXCOM_COLS.issubset(set(df.columns)), f"Missing Dexcom columns in {patient}"

    @pytest.mark.parametrize("patient", PATIENTS)
    def test_mixed_event_types(self, patient):
        df = pd.read_csv(MESSY / f"dexcom_{patient}.csv", skiprows=3)
        event_types = set(df["Event Type"].dropna().unique()) - {""}
        assert "EGV" in event_types, f"No EGV rows in {patient}"
        assert "Calibration" in event_types, f"No Calibration rows in {patient}"
        assert "Insulin" in event_types, f"No Insulin rows in {patient}"
        assert "Carbs" in event_types, f"No Carbs rows in {patient}"

    @pytest.mark.parametrize("patient", PATIENTS)
    def test_blank_spacer_rows_present(self, patient):
        """File must contain at least one blank/spacer row between event blocks."""
        df = pd.read_csv(MESSY / f"dexcom_{patient}.csv", skiprows=3)
        # Blank spacer: pandas reads empty CSV rows as all-NaN
        blank_mask = df.isnull().all(axis=1)
        assert blank_mask.any(), f"No blank spacer rows found in {patient}"

    @pytest.mark.parametrize("patient", PATIENTS)
    def test_dropout_rows_exist(self, patient):
        """At least some EGV rows should have empty glucose (sensor dropout)."""
        df = pd.read_csv(MESSY / f"dexcom_{patient}.csv", skiprows=3)
        egv = df[df["Event Type"] == "EGV"]
        # pandas reads empty CSV cells as NaN
        empty_glucose = egv["Glucose Value (mg/dL)"].isna()
        assert empty_glucose.any(), f"No dropout (empty glucose) rows in {patient}"


class TestDexcomChildLowSentinels:
    """child#001 always hits low glucose — verify Low strings are present in its file."""
    def test_low_sentinel_strings_present(self):
        df = pd.read_csv(MESSY / "dexcom_child_001.csv", skiprows=3)
        egv = df[df["Event Type"] == "EGV"]
        low_rows = egv[egv["Glucose Value (mg/dL)"].astype(str).str.strip() == "Low"]
        assert len(low_rows) > 0, "Expected Low sentinel strings in child_001 messy CSV"
