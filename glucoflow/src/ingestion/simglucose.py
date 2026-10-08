"""
simglucose.py
Ingests simglucose-generated CSVs from data/samples/synthetic/ into Bronze S3.

simglucose output columns (may vary slightly by version):
    BG      - Blood glucose (mg/dL) — the "true" physiological value
    CGM     - CGM sensor reading (mg/dL) — BG + sensor noise (what a real sensor reads)
    CHO     - Carbohydrate intake (grams)
    insulin - Insulin delivered (units)
    LBGI    - Low Blood Glucose Index (risk metric)
    HBGI    - High Blood Glucose Index (risk metric)
    Risk    - Composite risk score

We use the CGM column as glucose_mgdl (to match what a real sensor would capture),
and map CHO → carbs_grams, insulin → insulin_units.

Timestamps are synthesized from the row index (5-min intervals) since simglucose
doesn't write wall-clock timestamps by default.
"""

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.common.schema import CanonicalReading
from src.common.s3_utils import get_s3_client, build_key, upload_jsonl
from src.common.config import cfg

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

INPUT_DIR = Path(__file__).resolve().parents[2] / "data" / "samples" / "synthetic"
SIM_START = datetime(2023, 1, 1, 0, 0, 0)
INTERVAL_MINUTES = 5


def find_column(df: pd.DataFrame, aliases: list[str]) -> str | None:
    for alias in aliases:
        if alias in df.columns:
            return alias
    for alias in aliases:
        matches = [c for c in df.columns if alias.upper() in c.upper()]
        if matches:
            return matches[0]
    return None


def parse_simglucose_csv(csv_path: Path) -> tuple[list[CanonicalReading], list[dict]]:
    df = pd.read_csv(csv_path)
    patient_id = csv_path.stem  # e.g. "adult_001"

    cgm_col = find_column(df, ["CGM", "cbg", "BG"])

    if cgm_col is None:
        logger.error(f"Cannot find glucose column in {csv_path.name}. Columns: {list(df.columns)}")
        return [], []

    valid: list[CanonicalReading] = []
    failed: list[dict] = []

    for i, row in df.iterrows():
        ts = SIM_START + timedelta(minutes=INTERVAL_MINUTES * i)
        row_dict = row.to_dict()
        try:
            reading = CanonicalReading.from_raw(
                row_dict,
                patient_id=f"sim_{patient_id}",
                event_time=ts,
                source_system="simglucose",
                batch_or_stream="batch",
                glucose_raw=row[cgm_col],
            )
            valid.append(reading)
        except (ValidationError, ValueError, TypeError) as e:
            failed.append({"file": csv_path.name, "row": i, "error": str(e)})

    return valid, failed


def main():
    csv_files = sorted(INPUT_DIR.glob("*.csv"))
    if not csv_files:
        logger.error(f"No CSV files found in {INPUT_DIR}. Run 'make simulate' first.")
        sys.exit(1)

    s3 = get_s3_client(cfg.aws_region)

    for csv_path in csv_files:
        logger.info(f"Parsing {csv_path.name}...")
        valid, failed = parse_simglucose_csv(csv_path)
        if failed:
            logger.warning(f"  {len(failed)} rows failed validation")
        if valid:
            patient_id = csv_path.stem
            key = build_key("simglucose", f"{patient_id}.jsonl")
            upload_jsonl(s3, [r.model_dump(mode="json") for r in valid], cfg.bronze_bucket, key)
            logger.info(f"  ✅ {len(valid)} rows → s3://{cfg.bronze_bucket}/{key}")


if __name__ == "__main__":
    main()
