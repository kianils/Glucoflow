"""
silver.py — GlucoFlow Silver transform layer

Reads Bronze JSONL from S3, applies quality rules, enriches each record with
clinical metadata, and writes the clean result to the Silver S3 bucket.
Invalid records are routed to the Quarantine bucket with a reason tag.

Quality rules applied here:
  1. Records with glucose_flag == "invalid" → Quarantine (no actionable value)
  2. Records with transform errors (missing fields, bad timestamps) → Quarantine
  3. All other records (in_range, out_of_range, low, high) → Silver

Clinical enrichment added at Silver:
  - clinical_range      : ADA-aligned label (severe_hypo → severe_hyper)
  - time_in_range_eligible : True only for numeric readings inside sensor range
  - hour_of_day         : 0–23 extracted from event_time (for diurnal patterns)
  - day_of_week         : "Monday"–"Sunday" (for weekly pattern analysis)
  - silver_processed_at : UTC timestamp of this transform run
  - silver_schema_version : "1.0" — version-gate for downstream consumers

Why Silver is a separate layer from Bronze:
  Bronze is append-only and faithful — every raw byte that came in is stored
  verbatim. Silver is where we make decisions: what is "clean enough" to query,
  how do we classify a reading clinically, what derived fields do analysts need?
  Keeping these layers separate means Bronze is always replayable — if the Silver
  quality rules change tomorrow, we can re-run this transform from scratch.

ADA 2023 glucose thresholds (mg/dL):
  < 54      → Level 2 hypoglycaemia (severe_hypo) — clinically urgent
  54–<70    → Level 1 hypoglycaemia (hypo)
  70–180    → Time In Range target (normal) — TIR target ≥70% for T1DM/T2DM
  180–250   → Level 1 hyperglycaemia (hyper)
  > 250     → Level 2 hyperglycaemia (severe_hyper)
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.common.config import cfg
from src.common.s3_utils import get_s3_client, upload_jsonl
from src.catalog.glue_catalog import ensure_database, register_silver_table

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  ADA 2023 clinical thresholds                                        #
# ------------------------------------------------------------------ #

SEVERE_HYPO_THRESHOLD = 54.0    # mg/dL — Level 2 hypoglycaemia boundary
HYPO_THRESHOLD        = 70.0    # mg/dL — Level 1 hypoglycaemia boundary
TIR_UPPER             = 180.0   # mg/dL — upper bound of Time In Range
HYPER_THRESHOLD       = 250.0   # mg/dL — Level 1/2 hyperglycaemia boundary

ClinicalRange = Literal[
    "severe_hypo",   # < 54 mg/dL   — ADA Level 2 hypo, immediate risk
    "hypo",          # 54–<70 mg/dL  — ADA Level 1 hypo, intervention needed
    "normal",        # 70–180 mg/dL  — Time In Range; clinical target
    "hyper",         # 180–250 mg/dL — ADA Level 1 hyper
    "severe_hyper",  # > 250 mg/dL   — ADA Level 2 hyper, high-risk zone
    "low_signal",    # Sensor reported "Low" — below sensor detection floor (~40)
    "high_signal",   # Sensor reported "High" — above sensor ceiling (~400)
    "no_signal",     # Invalid/missing — should never reach Silver normally
]


# ------------------------------------------------------------------ #
#  Silver schema                                                       #
# ------------------------------------------------------------------ #

class SilverReading(BaseModel):
    """
    A Bronze CanonicalReading enriched with clinical metadata.

    All Bronze fields are preserved verbatim so the Silver record is
    self-contained — no join back to Bronze is needed for audit.

    Frozen=True: Silver rows are immutable once created, matching Bronze's
    immutability contract. This prevents accidental field mutation in
    downstream Gold aggregation code.
    """

    model_config = ConfigDict(frozen=True)

    # --- Preserved Bronze fields (verbatim) ---
    patient_id:      str
    lineage_id:      str               # UUID4 from Bronze — unique row ID
    event_time:      datetime          # UTC-aware timestamp from device
    ingest_time:     datetime          # UTC-aware timestamp from Bronze ingest
    glucose_mgdl:    Optional[float]   # None for low/high/invalid flags
    glucose_flag:    str               # in_range | low | high | out_of_range | invalid
    source_system:   str               # shanghai | simglucose | dexcom_messy
    batch_or_stream: str               # batch | stream
    raw_payload:     dict              # verbatim original row from source

    # --- Silver enrichment ---
    clinical_range: ClinicalRange = Field(
        ...,
        description="ADA 2023 clinical classification of this glucose reading",
    )
    time_in_range_eligible: bool = Field(
        ...,
        description=(
            "True if this row should be counted in TIR calculations. "
            "Only numeric in_range readings qualify; low/high/invalid do not."
        ),
    )
    hour_of_day: int = Field(
        ..., ge=0, le=23,
        description="Hour extracted from event_time (0–23), for diurnal pattern queries",
    )
    day_of_week: str = Field(
        ...,
        description="Day name extracted from event_time (e.g. 'Monday'), for weekly pattern queries",
    )
    silver_processed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of the Silver transform run that produced this record",
    )
    silver_schema_version: str = Field(
        default="1.0",
        description="Silver schema version — downstream consumers can gate on this",
    )


# ------------------------------------------------------------------ #
#  Core transform functions                                            #
# ------------------------------------------------------------------ #

def _classify_clinical_range(
    glucose_mgdl: Optional[float],
    glucose_flag: str,
) -> ClinicalRange:
    """
    Map a (glucose_mgdl, glucose_flag) pair to an ADA-aligned clinical label.

    This is a pure function — no side effects, no I/O. Every branch is
    independently testable. The thresholds are module-level constants so
    they can be updated in one place if ADA guidelines change.

    Decision order:
      1. Check flag first — "low"/"high" are sensor-reported, not numeric
      2. Check for missing value (invalid flag or None glucose)
      3. Apply numeric thresholds in ascending order
    """
    if glucose_flag == "low":
        return "low_signal"
    if glucose_flag == "high":
        return "high_signal"
    if glucose_flag == "invalid" or glucose_mgdl is None:
        return "no_signal"

    # Numeric path — apply ADA thresholds
    if glucose_mgdl < SEVERE_HYPO_THRESHOLD:
        return "severe_hypo"
    if glucose_mgdl < HYPO_THRESHOLD:
        return "hypo"
    if glucose_mgdl <= TIR_UPPER:
        return "normal"
    if glucose_mgdl <= HYPER_THRESHOLD:
        return "hyper"
    return "severe_hyper"


def _parse_dt(raw) -> datetime:
    """Parse a datetime from either a string (ISO 8601) or an existing datetime.
    Always returns a UTC-aware datetime."""
    if isinstance(raw, datetime):
        dt = raw
    else:
        dt = datetime.fromisoformat(str(raw))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def transform_record(record: dict) -> Optional["SilverReading"]:
    """
    Transform a single Bronze JSONL record dict into a SilverReading.

    Returns:
        SilverReading — if the record passes quality rules
        None          — if the record should be quarantined (invalid flag)

    Raises:
        KeyError   — if a required Bronze field is missing
        ValueError — if a field cannot be parsed (e.g. bad timestamp)

    Why return None instead of raising?
    Invalid records are an expected, recoverable outcome — not an error.
    Callers use `result is None` to route to quarantine, while genuine
    structural errors (KeyError/ValueError) are caught separately and also
    quarantined but logged as warnings.
    """
    glucose_flag = record.get("glucose_flag", "invalid")

    # Quality gate: invalid readings go to quarantine, not Silver
    if glucose_flag == "invalid":
        return None

    glucose_mgdl   = record.get("glucose_mgdl")
    clinical_range = _classify_clinical_range(glucose_mgdl, glucose_flag)

    # TIR eligibility: only numeric readings inside the sensor's valid range
    time_in_range_eligible = (
        glucose_mgdl is not None
        and glucose_flag in ("in_range", "out_of_range")
    )

    event_time  = _parse_dt(record["event_time"])
    ingest_time = _parse_dt(record["ingest_time"])

    return SilverReading(
        patient_id              = record["patient_id"],
        lineage_id              = record["lineage_id"],
        event_time              = event_time,
        ingest_time             = ingest_time,
        glucose_mgdl            = glucose_mgdl,
        glucose_flag            = glucose_flag,
        source_system           = record["source_system"],
        batch_or_stream         = record["batch_or_stream"],
        raw_payload             = record.get("raw_payload", {}),
        clinical_range          = clinical_range,
        time_in_range_eligible  = time_in_range_eligible,
        hour_of_day             = event_time.hour,
        day_of_week             = event_time.strftime("%A"),
    )


def process_bronze_object(
    s3_client,
    bucket: str,
    key: str,
) -> tuple[list[SilverReading], list[dict]]:
    """
    Download a single Bronze JSONL object, transform every line.

    Returns (silver_records, quarantine_records).
    Malformed JSON lines are logged and counted as quarantine records.
    """
    obj = s3_client.get_object(Bucket=bucket, Key=key)
    raw = obj["Body"].read().decode("utf-8")

    silver: list[SilverReading] = []
    quarantine: list[dict]      = []

    for line_no, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue

        # Parse JSON
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning(f"  Line {line_no} in {key}: malformed JSON — {exc}")
            quarantine.append({"raw_line": line, "quarantine_reason": f"json_parse_error: {exc}"})
            continue

        # Transform
        try:
            result = transform_record(record)
        except (KeyError, ValueError) as exc:
            logger.warning(f"  Line {line_no} in {key}: transform error — {exc}")
            quarantine.append({**record, "quarantine_reason": f"transform_error: {exc}"})
            continue

        if result is None:
            # Quality filter: invalid glucose flag
            quarantine.append({**record, "quarantine_reason": "glucose_flag_invalid"})
        else:
            silver.append(result)

    return silver, quarantine


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #

def main():
    s3 = get_s3_client(cfg.aws_region)

    paginator = s3.get_paginator("list_objects_v2")
    pages     = paginator.paginate(Bucket=cfg.bronze_bucket)

    total_silver     = 0
    total_quarantine = 0
    files_processed  = 0

    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".jsonl"):
                continue

            logger.info(f"Processing Bronze: {key}")
            silver_records, quarantine_records = process_bronze_object(
                s3, cfg.bronze_bucket, key
            )
            files_processed += 1

            if silver_records:
                upload_jsonl(
                    s3,
                    [r.model_dump(mode="json") for r in silver_records],
                    cfg.silver_bucket,
                    key,                   # preserve partition key structure
                )
                logger.info(
                    f"  ✅ {len(silver_records)} rows → "
                    f"s3://{cfg.silver_bucket}/{key}"
                )
                total_silver += len(silver_records)

            if quarantine_records:
                quarantine_key = f"reason=quality_filter/{key}"
                upload_jsonl(
                    s3,
                    quarantine_records,
                    cfg.quarantine_bucket,
                    quarantine_key,
                )
                logger.warning(
                    f"  ⚠️  {len(quarantine_records)} quarantined → "
                    f"s3://{cfg.quarantine_bucket}/{quarantine_key}"
                )
                total_quarantine += len(quarantine_records)

    logger.info("")
    logger.info("=== Silver Transform Complete ===")
    logger.info(f"Files processed:  {files_processed}")
    logger.info(f"Silver rows:      {total_silver}")
    logger.info(f"Quarantined rows: {total_quarantine}")
    if total_silver + total_quarantine > 0:
        pass_rate = total_silver / (total_silver + total_quarantine) * 100
        logger.info(f"Pass rate:        {pass_rate:.1f}%")
    else:
        logger.info("No JSONL objects found in Bronze bucket.")

    # Auto-register the Silver table in Glue so Athena can query it immediately.
    # This is idempotent — safe to call every run.
    # Without this step, gold.py's Athena query fails with "table not found".
    if total_silver > 0:
        logger.info("")
        logger.info("Registering Silver table in Glue catalog...")
        glue = __import__("boto3").client("glue", region_name=cfg.aws_region)
        s3_location = f"s3://{cfg.silver_bucket}/"
        ensure_database("glucoflow_db", client=glue)
        register_silver_table("glucoflow_db", "silver_readings", s3_location, client=glue)
        logger.info("  ✅ Glue table glucoflow_db.silver_readings registered/updated")


if __name__ == "__main__":
    main()
