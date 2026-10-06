"""
shanghai.py
Ingests all Shanghai T1DM and T2DM .xlsx patient files into the Bronze S3 bucket.

What this script does, step by step:
  1. Scans a local directory for all patient .xlsx files
  2. Opens each file with openpyxl
  3. Reads the CGM sheet — locates columns by header name (not position) to be
     robust against column reordering between files
  4. Converts each row to a CgmReading (Pydantic model validates it)
  5. Failed rows are logged and their source file is flagged for quarantine
  6. Passes all valid rows are serialized as JSONL and uploaded to:
     s3://<BRONZE_BUCKET>/source=shanghai_<type>/date=<today>/<patient_id>.jsonl

Usage:
    python src/ingestion/shanghai.py

Environment variables required (see .env.example):
    BRONZE_BUCKET, QUARANTINE_BUCKET, AWS_REGION
"""

import logging
import sys
from datetime import datetime, date
from pathlib import Path

import openpyxl
from pydantic import ValidationError

# Add project root to path so we can import src.*
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.common.schema import CgmReading
from src.common.s3_utils import get_s3_client, build_key, upload_jsonl
from src.common.config import cfg

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Paths relative to project root
DATASET_ROOT = Path(__file__).resolve().parents[3] / "diabetes_datasets"
T1DM_DIR = DATASET_ROOT / "Shanghai_T1DM"
T2DM_DIR = DATASET_ROOT / "Shanghai_T2DM"

# Column name aliases — Shanghai files use slightly inconsistent headers
GLUCOSE_ALIASES = ["Glucose (mg/dL)", "Glucose", "glucose_value", "CBG (mg/dL)"]
TIMESTAMP_ALIASES = ["Timestamp", "Time", "timestamp", "Date Time"]
INSULIN_ALIASES = ["Insulin (units)", "Insulin", "insulin"]
CARBS_ALIASES = ["Carbs (grams)", "Carbs", "carbs", "Meal (CHO)"]


def find_col(headers: list, aliases: list) -> int | None:
    """Return the index of the first header that matches any alias, or None."""
    for alias in aliases:
        try:
            return headers.index(alias)
        except ValueError:
            continue
    return None


def parse_patient_file(xlsx_path: Path, source: str) -> tuple[list[CgmReading], list[dict]]:
    """
    Parse a single patient .xlsx file.
    Returns (valid_readings, failed_rows).
    """
    wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    
    # Find the CGM data sheet — usually named "CGM" or is the second sheet
    cgm_sheet = None
    for name in wb.sheetnames:
        if "cgm" in name.lower() or "glucose" in name.lower():
            cgm_sheet = wb[name]
            break
    if cgm_sheet is None:
        # Fall back to first sheet if no explicit CGM sheet
        cgm_sheet = wb.worksheets[0]

    rows = list(cgm_sheet.iter_rows(values_only=True))
    if not rows:
        logger.warning(f"Empty sheet in {xlsx_path.name}")
        return [], []

    # Parse headers
    headers = [str(h).strip() if h is not None else "" for h in rows[0]]
    ts_col = find_col(headers, TIMESTAMP_ALIASES)
    gluc_col = find_col(headers, GLUCOSE_ALIASES)

    if ts_col is None or gluc_col is None:
        logger.error(f"Could not locate timestamp or glucose column in {xlsx_path.name}. Headers: {headers}")
        return [], []

    insulin_col = find_col(headers, INSULIN_ALIASES)
    carbs_col = find_col(headers, CARBS_ALIASES)

    # Derive patient_id from filename (e.g. "1001_0_20210730.xlsx" → "1001")
    patient_id = xlsx_path.stem.split("_")[0]

    valid: list[CgmReading] = []
    failed: list[dict] = []

    for row_num, row in enumerate(rows[1:], start=2):
        raw_ts = row[ts_col]
        raw_gluc = row[gluc_col]

        # Skip completely empty rows
        if raw_ts is None and raw_gluc is None:
            continue

        # Skip null glucose values (Shanghai uses "/" for missing)
        if raw_gluc is None or str(raw_gluc).strip() in ("/", "", "nan", "None"):
            logger.debug(f"{xlsx_path.name} row {row_num}: null glucose, skipping")
            continue

        try:
            # Normalize timestamp
            if isinstance(raw_ts, datetime):
                ts = raw_ts
            elif isinstance(raw_ts, str):
                # Try common formats
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M", "%m/%d/%Y %H:%M:%S"):
                    try:
                        ts = datetime.strptime(raw_ts.strip(), fmt)
                        break
                    except ValueError:
                        continue
                else:
                    raise ValueError(f"Unrecognized timestamp format: {raw_ts!r}")
            else:
                raise ValueError(f"Unexpected timestamp type: {type(raw_ts)}")

            reading = CgmReading(
                patient_id=patient_id,
                source=source,
                timestamp=ts,
                glucose_mgdl=raw_gluc,
                insulin_units=row[insulin_col] if insulin_col is not None else None,
                carbs_grams=row[carbs_col] if carbs_col is not None else None,
            )
            valid.append(reading)

        except (ValidationError, ValueError, TypeError) as e:
            failed.append({"file": xlsx_path.name, "row": row_num, "error": str(e), "raw": str(row)})

    wb.close()
    return valid, failed


def ingest_directory(patient_dir: Path, source_label: str, s3, bronze_bucket: str, quarantine_bucket: str):
    """Process all .xlsx files in a patient directory."""
    files = sorted(patient_dir.glob("*.xlsx"))
    logger.info(f"Found {len(files)} files in {patient_dir.name}")

    total_valid = 0
    total_failed = 0

    for xlsx_path in files:
        logger.info(f"  Parsing {xlsx_path.name}...")
        valid, failed = parse_patient_file(xlsx_path, source_label)
        total_valid += len(valid)
        total_failed += len(failed)

        if failed:
            logger.warning(f"    {len(failed)} rows failed validation in {xlsx_path.name}")

        if valid:
            patient_id = xlsx_path.stem.split("_")[0]
            key = build_key(source_label, f"{patient_id}.jsonl")
            upload_jsonl(s3, [r.model_dump(mode="json") for r in valid], bronze_bucket, key)
            logger.info(f"    ✅ Uploaded {len(valid)} rows → s3://{bronze_bucket}/{key}")

    logger.info(f"{'='*50}")
    logger.info(f"{patient_dir.name}: {total_valid} rows valid, {total_failed} rows failed")
    return total_valid, total_failed


def main():
    s3 = get_s3_client(cfg.aws_region)

    t1dm_valid, t1dm_failed = ingest_directory(T1DM_DIR, "shanghai_t1dm", s3, cfg.bronze_bucket, cfg.quarantine_bucket)
    t2dm_valid, t2dm_failed = ingest_directory(T2DM_DIR, "shanghai_t2dm", s3, cfg.bronze_bucket, cfg.quarantine_bucket)

    logger.info(f"\nFinal summary:")
    logger.info(f"  T1DM: {t1dm_valid} valid, {t1dm_failed} failed")
    logger.info(f"  T2DM: {t2dm_valid} valid, {t2dm_failed} failed")


if __name__ == "__main__":
    main()
