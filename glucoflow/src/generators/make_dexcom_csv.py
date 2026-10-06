"""
make_dexcom_csv.py
==================
Takes the clean simglucose CSVs from data/samples/synthetic/ and transforms
them into files that mimic real Dexcom Clarity exports — the intentionally
messy format we use to stress-test the ingestion parser.

What makes Dexcom Clarity CSVs messy (and why each quirk is deliberately included):

1. METADATA HEADER (3 rows before the actual data)
   Real Clarity exports start with device info rows, not column headers.
   Our parser must skip these without hard-coding row numbers.

2. MIXED EVENT TYPES in a single column
   Dexcom writes glucose readings, calibration finger-pricks, insulin doses
   and carb entries all into the same "Event Type" column. Each row has mostly
   empty cells for the fields that don't apply to it. Our parser must
   route each event type correctly.

3. "Low" / "High" SENTINEL STRINGS instead of numbers
   When the sensor hits its lower limit (~40 mg/dL) it writes the string "Low".
   Upper limit (~400 mg/dL) → "High". Our schema's _classify_glucose() handles this.

4. MMOL/L COLUMN alongside mg/dL
   Clarity can export in either unit system. We include both, which our parser
   should ignore (always use mg/dL).

5. EMPTY/NaN ROWS between event blocks
   Clarity inserts blank rows between the glucose section and calibration section.
   Our parser must skip them without error.

Usage:
    python src/generators/make_dexcom_csv.py
    # or via Makefile:
    make make-dexcom

Inputs:
    data/samples/synthetic/<patient_name>.csv

Outputs:
    data/samples/messy/dexcom_<patient_name>.csv
"""

import logging
import random
from pathlib import Path

import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SRC_DIR  = Path(__file__).resolve().parents[2] / "data" / "samples" / "synthetic"
OUT_DIR  = Path(__file__).resolve().parents[2] / "data" / "samples" / "messy"

# Dexcom Clarity column headers (real export names)
CLARITY_COLS = [
    "Index",
    "Timestamp (YYYY-MM-DDThh:mm:ss)",
    "Event Type",
    "Event Subtype",
    "Patient Info",
    "Device Info",
    "Source Device ID",
    "Glucose Value (mg/dL)",
    "Insulin Value (u)",
    "Carb Value (grams)",
    "Duration (hh:mm:ss)",
    "Glucose Rate of Change (mg/dL/min)",
    "Transmitter Time (Long Integer)",
    "Transmitter ID",
]

# Probability that a given EGV row is a "Low" or "High" string rather than numeric.
# We inject these at rows where the sim value is near the boundary to be realistic.
LOW_STR_THRESHOLD  = 55.0   # below this, randomly replace with "Low"
HIGH_STR_THRESHOLD = 350.0  # above this, randomly replace with "High"

# Fraction of glucose rows to blank out entirely (sensor dropout)
DROPOUT_RATE = 0.02


def _mgdl_to_mmol(v):
    """Convert mg/dL to mmol/L (for the extra column Clarity provides)."""
    return round(v / 18.018, 1) if v is not None else None


def _glucose_cell(value: float) -> str:
    """Apply Dexcom-style Low/High substitution and dropout."""
    if random.random() < DROPOUT_RATE:
        return ""
    if value < LOW_STR_THRESHOLD and random.random() < 0.7:
        return "Low"
    if value > HIGH_STR_THRESHOLD and random.random() < 0.7:
        return "High"
    return str(round(value, 1))


def _make_metadata_header(patient_name: str) -> list[str]:
    """
    Return 3 lines mimicking a real Clarity metadata block.
    These appear BEFORE the column header row in the file.
    """
    return [
        f"Patient Name: [REDACTED],Glucose Trend: --,Device: Dexcom G6,Serial Number: SIM{patient_name[:8].upper()}00",
        f"Exported: 2021-07-30T00:00:00,Software Version: 3.3.0.14,Unit: mg/dL,",
        ",,,,,",  # blank spacer row (also common in real exports)
    ]


def _build_egv_rows(df: pd.DataFrame) -> list[dict]:
    """Convert simglucose rows into Dexcom EGV (Estimated Glucose Value) event rows."""
    rows = []
    for i, row in df.iterrows():
        glucose_str = _glucose_cell(row["cgm_mgdl"])
        rows.append({
            "Index": i,
            "Timestamp (YYYY-MM-DDThh:mm:ss)": pd.Timestamp(row["timestamp"]).strftime("%Y-%m-%dT%H:%M:%S"),
            "Event Type": "EGV",
            "Event Subtype": "",
            "Patient Info": "",
            "Device Info": "Dexcom G6",
            "Source Device ID": f"SIM-{row['patient_id']}",
            "Glucose Value (mg/dL)": glucose_str,
            "Insulin Value (u)": "",
            "Carb Value (grams)": "",
            "Duration (hh:mm:ss)": "",
            "Glucose Rate of Change (mg/dL/min)": "",
            "Transmitter Time (Long Integer)": int(i) * 180,
            "Transmitter ID": f"SIM{str(i)[:6].zfill(6)}",
        })
    return rows


def _build_calibration_rows(df: pd.DataFrame) -> list[dict]:
    """
    Inject ~3 calibration rows (finger-prick readings) spread across the day.
    Calibration rows have Event Type = "Calibration" and a slightly different
    glucose value from the CGM (finger-pricks are more accurate).
    """
    indices = sorted(random.sample(range(len(df)), min(3, len(df))))
    rows = []
    for idx in indices:
        row = df.iloc[idx]
        # Finger-prick: no sensor noise, but we add tiny human measurement error
        finger_value = round(float(row["cgm_mgdl"]) + random.gauss(0, 3), 1)
        rows.append({
            "Index": "",
            "Timestamp (YYYY-MM-DDThh:mm:ss)": pd.Timestamp(row["timestamp"]).strftime("%Y-%m-%dT%H:%M:%S"),
            "Event Type": "Calibration",
            "Event Subtype": "",
            "Patient Info": "",
            "Device Info": "Dexcom G6",
            "Source Device ID": f"SIM-{row['patient_id']}",
            "Glucose Value (mg/dL)": str(finger_value),
            "Insulin Value (u)": "",
            "Carb Value (grams)": "",
            "Duration (hh:mm:ss)": "",
            "Glucose Rate of Change (mg/dL/min)": "",
            "Transmitter Time (Long Integer)": "",
            "Transmitter ID": "",
        })
    return rows


def _build_insulin_rows(df: pd.DataFrame) -> list[dict]:
    """Extract rows where insulin was delivered and write as separate Insulin event rows."""
    rows = []
    for _, row in df[df["insulin_units"] > 0].iterrows():
        rows.append({
            "Index": "",
            "Timestamp (YYYY-MM-DDThh:mm:ss)": pd.Timestamp(row["timestamp"]).strftime("%Y-%m-%dT%H:%M:%S"),
            "Event Type": "Insulin",
            "Event Subtype": "Fast-Acting",
            "Patient Info": "",
            "Device Info": "Insulet OmniPod",
            "Source Device ID": f"SIM-{row['patient_id']}",
            "Glucose Value (mg/dL)": "",
            "Insulin Value (u)": round(float(row["insulin_units"]), 3),
            "Carb Value (grams)": "",
            "Duration (hh:mm:ss)": "",
            "Glucose Rate of Change (mg/dL/min)": "",
            "Transmitter Time (Long Integer)": "",
            "Transmitter ID": "",
        })
    return rows


def _build_carb_rows(df: pd.DataFrame) -> list[dict]:
    """Extract rows where carbs were consumed and write as separate Carbs event rows."""
    rows = []
    for _, row in df[df["cho_grams"] > 0].iterrows():
        rows.append({
            "Index": "",
            "Timestamp (YYYY-MM-DDThh:mm:ss)": pd.Timestamp(row["timestamp"]).strftime("%Y-%m-%dT%H:%M:%S"),
            "Event Type": "Carbs",
            "Event Subtype": "",
            "Patient Info": "",
            "Device Info": "",
            "Source Device ID": f"SIM-{row['patient_id']}",
            "Glucose Value (mg/dL)": "",
            "Insulin Value (u)": "",
            "Carb Value (grams)": round(float(row["cho_grams"]), 1),
            "Duration (hh:mm:ss)": "",
            "Glucose Rate of Change (mg/dL/min)": "",
            "Transmitter Time (Long Integer)": "",
            "Transmitter ID": "",
        })
    return rows


def convert_patient(src_path: Path, out_dir: Path) -> Path:
    """Convert one simglucose CSV → one Dexcom-style messy CSV."""
    patient_name = src_path.stem  # e.g. "adolescent_001"
    logger.info(f"Converting {src_path.name} → dexcom_{patient_name}.csv")

    df = pd.read_csv(src_path)
    random.seed(42)
    np.random.seed(42)

    # Build all four event type blocks
    egv_rows  = _build_egv_rows(df)
    cal_rows  = _build_calibration_rows(df)
    ins_rows  = _build_insulin_rows(df)
    carb_rows = _build_carb_rows(df)

    # Assemble: EGV block, blank spacer, Calibration block, blank spacer, Insulin+Carbs
    blank_row = {c: "" for c in CLARITY_COLS}
    all_rows  = (
        egv_rows
        + [blank_row]
        + cal_rows
        + [blank_row]
        + ins_rows
        + carb_rows
    )

    events_df = pd.DataFrame(all_rows, columns=CLARITY_COLS)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"dexcom_{patient_name}.csv"

    # Write metadata header lines first, then the DataFrame
    with open(out_path, "w") as f:
        for meta_line in _make_metadata_header(patient_name):
            f.write(meta_line + "\n")
        events_df.to_csv(f, index=False)

    n_egv = len(egv_rows)
    n_low = sum(1 for r in egv_rows if r["Glucose Value (mg/dL)"] == "Low")
    n_high = sum(1 for r in egv_rows if r["Glucose Value (mg/dL)"] == "High")
    n_blank = sum(1 for r in egv_rows if r["Glucose Value (mg/dL)"] == "")
    logger.info(
        f"  → {out_path.name}  |  {n_egv} EGV  |  {len(cal_rows)} cal  "
        f"|  {len(ins_rows)} insulin  |  {len(carb_rows)} carb  "
        f"|  Low={n_low}  High={n_high}  dropout={n_blank}"
    )
    return out_path


def main():
    src_files = sorted(SRC_DIR.glob("*.csv"))
    if not src_files:
        logger.error(f"No synthetic CSVs found in {SRC_DIR}. Run 'make simulate' first.")
        return

    logger.info(f"Converting {len(src_files)} synthetic CSV(s) → messy Dexcom format")
    for src in src_files:
        convert_patient(src, OUT_DIR)
    logger.info("Done.")


if __name__ == "__main__":
    main()
