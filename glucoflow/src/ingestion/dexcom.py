"""
dexcom.py
Ingests Dexcom Clarity-style messy CSV exports from data/samples/messy/ into Bronze S3.

This parser deliberately handles every known quirk of the Dexcom Clarity format:

  1. METADATA HEADER: Skip the first 3 rows (device info, patient info, blank line)
     before the actual column names appear on row 4.

  2. MIXED EVENT TYPES: A single file contains EGV rows (glucose), Calibration rows
     (finger-prick), InsulinDelivery rows, and Food rows. We filter to EGV + Calibration
     for this project; the others are preserved in the raw Bronze upload.

  3. LOW/HIGH STRINGS: Values below 40 mg/dL are written as "Low"; above 400 as "High".
     The CanonicalReading schema validator handles conversion (Low → 39.0, High → 401.0).

  4. TRANSMITTER ID: Included in output for device provenance tracking.

  5. CALIBRATION FLAG: Calibration rows set is_calibration=True in raw_payload.

All rows (EGV + Calibration) that pass schema validation go to Bronze.
Other event types (insulin, food) are dropped at Bronze stage — the pipeline
can be extended to ingest those into separate schemas later.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.common.schema import CanonicalReading
from src.common.s3_utils import get_s3_client, build_key, upload_jsonl
from src.common.config import cfg

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

INPUT_DIR = Path(__file__).resolve().parents[2] / "data" / "samples" / "messy"

GLUCOSE_EVENTS = {"EGV", "egv", "Calibration", "calibration"}
TIMESTAMP_COL = "Timestamp (YYYY-MM-DDThh:mm:ss)"
GLUCOSE_COL = "Glucose Value (mg/dL)"
EVENT_TYPE_COL = "Event Type"
INSULIN_COL = "Insulin Value (u)"
CARBS_COL = "Carb Value (grams)"


def parse_dexcom_export(csv_path: Path) -> tuple[list[CanonicalReading], list[dict], list[dict]]:
    """
    Parse a Dexcom Clarity CSV export.
    Returns (valid_readings, failed_rows, skipped_event_rows)
    """
    # Skip the 3 metadata rows; row 4 = column headers
    try:
        df = pd.read_csv(csv_path, skiprows=3, dtype=str)
    except Exception as e:
        logger.error(f"Cannot read {csv_path.name}: {e}")
        return [], [], []

    if TIMESTAMP_COL not in df.columns or GLUCOSE_COL not in df.columns:
        logger.error(f"Expected columns not found in {csv_path.name}. Got: {list(df.columns[:8])}")
        return [], [], []

    patient_id = csv_path.stem.replace("_dexcom_export", "")

    valid: list[CanonicalReading] = []
    failed: list[dict] = []
    skipped: list[dict] = []

    for i, row in df.iterrows():
        event_type = str(row.get(EVENT_TYPE_COL, "")).strip()

        if event_type not in GLUCOSE_EVENTS:
            skipped.append({"row": i, "event_type": event_type})
            continue

        raw_ts = str(row[TIMESTAMP_COL]).strip()
        raw_gluc = str(row[GLUCOSE_COL]).strip()
        is_calibration = "calibration" in event_type.lower()

        if raw_ts in ("nan", "", "None") or raw_gluc in ("nan", "", "None"):
            continue

        try:
            # Parse ISO 8601 timestamp
            ts = datetime.fromisoformat(raw_ts)

            # Build raw payload with all fields for traceability
            row_dict = row.to_dict()
            row_dict["is_calibration"] = is_calibration
            if INSULIN_COL in df.columns:
                ins_val = str(row.get(INSULIN_COL, "")).strip()
                if ins_val not in ("nan", "", "None"):
                    row_dict["insulin_units"] = float(ins_val)
            if CARBS_COL in df.columns:
                carb_val = str(row.get(CARBS_COL, "")).strip()
                if carb_val not in ("nan", "", "None"):
                    row_dict["carbs_grams"] = float(carb_val)

            reading = CanonicalReading.from_raw(
                row_dict,
                patient_id=f"dexcom_{patient_id}",
                event_time=ts,
                source_system="dexcom_messy",
                batch_or_stream="batch",
                glucose_raw=raw_gluc,
            )
            valid.append(reading)

        except (ValidationError, ValueError, TypeError) as e:
            failed.append({"file": csv_path.name, "row": i, "error": str(e), "raw_glucose": raw_gluc, "raw_ts": raw_ts})

    return valid, failed, skipped


def main():
    csv_files = sorted(INPUT_DIR.glob("dexcom_*.csv"))
    if not csv_files:
        logger.error(f"No Dexcom export CSVs found in {INPUT_DIR}.")
        logger.error("Run 'make make-dexcom' first.")
        sys.exit(1)

    s3 = get_s3_client(cfg.aws_region)

    for csv_path in csv_files:
        logger.info(f"Parsing {csv_path.name}...")
        valid, failed, skipped = parse_dexcom_export(csv_path)
        logger.info(f"  EGV/Calibration valid: {len(valid)}, failed: {len(failed)}, other events skipped: {len(skipped)}")
        if valid:
            patient_id = csv_path.stem.replace("_dexcom_export", "")
            key = build_key("dexcom_messy", f"{patient_id}.jsonl")
            upload_jsonl(s3, [r.model_dump(mode="json") for r in valid], cfg.bronze_bucket, key)
            logger.info(f"  ✅ {len(valid)} rows → s3://{cfg.bronze_bucket}/{key}")


if __name__ == "__main__":
    main()
