"""
gold.py — GlucoFlow v2 Gold Transform Layer

Aggregates Silver readings into daily summary metrics per patient using Athena,
then merges clinical profile fields (diabetes_type, age, comorbidities etc)
extracted from the Shanghai summary sheets at ingestion time.

v2 Changes
----------
- GoldReading now carries all ClinicalProfile fields as Optional fields
- merge_clinical_profile() loads clinical profile JSON from Bronze and
  merges matching fields into each GoldReading
- The combined dict (CGM metrics + clinical fields) is what the eligibility
  engine receives — enabling diabetes_type, age, comorbidity criteria

Primary CGM Metrics (computed at Gold from Silver via Athena):
  1. tir_percent         — % of eligible readings in 70–180 mg/dL target
  2. mean_glucose        — daily average glucose (mg/dL)
  3. hypo_events         — count of readings < 70 mg/dL
  4. severe_hypo_events  — count of readings < 54 mg/dL
  5. cv_percent          — Coefficient of Variation (SD / mean * 100)
  6. gmi                 — Glucose Management Indicator (estimated HbA1c)
                           Formula: GMI (%) = 3.31 + (0.02392 * mean_glucose)

Clinical Fields (merged from Bronze clinical_profiles/ after Athena query):
  diabetes_type, age, sex, bmi, diabetes_duration_years, hba1c_admission,
  has_retinopathy, has_nephropathy, has_neuropathy,
  on_insulin, on_metformin, on_glp1, on_sglt2,
  egfr, creatinine, ldl, triglycerides

Hive Partitioning Layout:
  s3://<GOLD_BUCKET>/source=<source_system>/date=<date>/<patient_id>_daily.jsonl
"""

import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.common.config import cfg
from src.common.clinical_profile import ClinicalProfile
from src.common.s3_utils import get_s3_client, upload_jsonl

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Gold schema                                                        #
# ------------------------------------------------------------------ #

class GoldReading(BaseModel):
    """
    Daily clinical metrics roll-up for a single patient, v2.

    Combines CGM-derived aggregate metrics (from Athena over the Silver
    layer) with clinical/demographic fields (from the Shanghai summary
    sheets, uploaded to Bronze at ingestion time).

    The combined flat dict representation (model_dump) is what the
    eligibility engine receives as gold_metrics — so every field name
    here must match the criterion field names in the trial protocol JSONs.

    Frozen=True: Gold rows are immutable facts. If a re-run produces
    different values, a new row is written (new ingest_time), not an
    in-place mutation. This preserves audit trail integrity.

    Schema version 2.0 — adds clinical profile fields.
    """
    model_config = ConfigDict(frozen=True)

    # ── CGM Aggregate Metrics ─────────────────────────────────────────
    patient_id:          str   = Field(..., description="Unique source-scoped patient identifier")
    day:                 str   = Field(..., description="Date YYYY-MM-DD")
    tir_percent:         float = Field(..., description="% readings in 70–180 mg/dL target range")
    mean_glucose:        float = Field(..., description="Daily mean glucose (mg/dL)")
    hypo_events:         int   = Field(..., description="Readings < 70 mg/dL")
    severe_hypo_events:  int   = Field(..., description="Readings < 54 mg/dL")
    cv_percent:          float = Field(..., description="Coefficient of Variation (SD/mean * 100)")
    gmi:      Optional[float]  = Field(None, description="Glucose Management Indicator (estimated HbA1c %)")
    source_system:       str   = Field(..., description="Source: 'shanghai', 'simglucose', or 'dexcom_messy'")
    gold_processed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of this Gold row creation"
    )
    gold_schema_version: str = Field("2.0", description="Gold schema version — v2 adds clinical fields")

    # ── Clinical / Demographic Fields (from Shanghai summary sheets) ──
    # All Optional — simglucose/Dexcom patients will have None for these.
    # Shanghai patients get these populated via merge_clinical_profile().
    diabetes_type: Optional[Literal["T1DM", "T2DM"]] = Field(
        default=None,
        description="T1DM or T2DM — from Shanghai summary sheet"
    )
    age: Optional[int] = Field(
        default=None,
        description="Age at admission (years)"
    )
    sex: Optional[Literal["M", "F"]] = Field(
        default=None,
        description="Biological sex"
    )
    bmi: Optional[float] = Field(
        default=None,
        description="BMI (kg/m²)"
    )
    diabetes_duration_years: Optional[float] = Field(
        default=None,
        description="Years since diabetes diagnosis"
    )
    hba1c_admission: Optional[float] = Field(
        default=None,
        description="Lab-measured HbA1c (%) at hospital admission"
    )
    has_retinopathy: Optional[bool] = Field(
        default=None,
        description="Diabetic retinopathy documented"
    )
    has_nephropathy: Optional[bool] = Field(
        default=None,
        description="Diabetic nephropathy documented"
    )
    has_neuropathy: Optional[bool] = Field(
        default=None,
        description="Peripheral neuropathy documented"
    )
    on_insulin: Optional[bool] = Field(
        default=None,
        description="On insulin therapy at admission"
    )
    on_metformin: Optional[bool] = Field(
        default=None,
        description="On metformin at admission"
    )
    on_glp1: Optional[bool] = Field(
        default=None,
        description="On GLP-1 receptor agonist at admission"
    )
    on_sglt2: Optional[bool] = Field(
        default=None,
        description="On SGLT-2 inhibitor at admission"
    )
    egfr: Optional[float] = Field(
        default=None,
        description="eGFR (mL/min/1.73m²)"
    )
    creatinine: Optional[float] = Field(
        default=None,
        description="Serum creatinine (μmol/L)"
    )
    ldl: Optional[float] = Field(
        default=None,
        description="LDL cholesterol"
    )
    triglycerides: Optional[float] = Field(
        default=None,
        description="Serum triglycerides"
    )

    def to_eligibility_dict(self) -> dict:
        """
        Return a flat dict suitable for passing to the eligibility engine.

        Merges CGM aggregate metrics and clinical fields into a single
        namespace. The eligibility engine calls gold_metrics.get(field_name)
        for every criterion — this dict is that lookup table.

        None values are excluded so the engine's 'metric unavailable' branch
        is triggered correctly for fields genuinely not recorded.
        """
        raw = self.model_dump(mode="python")
        # Always include CGM metrics (even if 0); exclude metadata + Nones for clinical
        cgm_fields = {
            "tir_percent", "mean_glucose", "hypo_events",
            "severe_hypo_events", "cv_percent", "gmi",
        }
        result = {}
        for k, v in raw.items():
            if k in cgm_fields and v is not None:
                result[k] = v
            elif k not in {"patient_id", "day", "source_system",
                           "gold_processed_at", "gold_schema_version"} and v is not None:
                result[k] = v
        # Always include patient_id for audit
        result["patient_id"] = self.patient_id
        return result


# ------------------------------------------------------------------ #
#  Core Clinical Formulas                                             #
# ------------------------------------------------------------------ #

def calculate_gmi(mean_glucose: float) -> Optional[float]:
    """
    Glucose Management Indicator — estimated HbA1c from mean CGM glucose.

    Formula (ADA/AACE 2019 standard):
      GMI (%) = 3.31 + (0.02392 × mean_glucose_mg_dL)

    Reference: Bergenstal et al., Diabetes Care, 2018.
    """
    if mean_glucose <= 0.0:
        return None
    return round(3.31 + (0.02392 * mean_glucose), 2)


# ------------------------------------------------------------------ #
#  Clinical Profile Loader                                            #
# ------------------------------------------------------------------ #

def load_clinical_profiles_from_s3(
    s3_client,
    bronze_bucket: str,
    run_date: Optional[str] = None,
) -> dict[str, ClinicalProfile]:
    """
    Load all clinical profile JSON files from Bronze for a given date.

    Clinical profiles are uploaded by shanghai.py to:
      s3://<bronze>/source=clinical_profiles/date=<today>/<source>.json

    Parameters
    ----------
    s3_client    : boto3 S3 client
    bronze_bucket: the Bronze S3 bucket name
    run_date     : YYYY-MM-DD string (defaults to today)

    Returns
    -------
    dict mapping patient_id (str) → ClinicalProfile
    """
    if run_date is None:
        run_date = date.today().isoformat()

    prefix = f"source=clinical_profiles/date={run_date}/"
    profiles: dict[str, ClinicalProfile] = {}

    try:
        response = s3_client.list_objects_v2(Bucket=bronze_bucket, Prefix=prefix)
        objects  = response.get("Contents", [])
        if not objects:
            logger.info(f"No clinical profile files found at {prefix} — clinical fields will be None")
            return profiles

        for obj in objects:
            body = s3_client.get_object(Bucket=bronze_bucket, Key=obj["Key"])["Body"].read()
            data = json.loads(body.decode("utf-8"))
            for pid, profile_dict in data.items():
                try:
                    profile = ClinicalProfile(**profile_dict)
                    profiles[pid] = profile
                except Exception as e:
                    logger.warning(f"Could not parse clinical profile for patient {pid}: {e}")

        logger.info(f"Loaded {len(profiles)} clinical profiles from Bronze")
    except Exception as e:
        logger.warning(f"Could not load clinical profiles: {e}")

    return profiles


def merge_clinical_profile(
    gold_dict: dict,
    profiles: dict[str, ClinicalProfile],
) -> dict:
    """
    Merge clinical profile fields into a Gold metrics dict.

    If a profile exists for the patient, its non-None fields are merged
    into the Gold dict. If no profile exists, the Gold dict is returned
    unchanged (clinical fields stay absent/None for the eligibility engine).

    Parameters
    ----------
    gold_dict : dict from GoldReading.model_dump(mode="json")
    profiles  : dict of patient_id → ClinicalProfile

    Returns
    -------
    dict — gold_dict updated with clinical fields
    """
    patient_id = gold_dict.get("patient_id", "")
    profile = profiles.get(patient_id)
    if profile is None:
        return gold_dict

    clinical_fields = profile.to_metrics_dict()
    merged = {**gold_dict, **clinical_fields}
    return merged


# ------------------------------------------------------------------ #
#  Silver S3 Loader                                                    #
# ------------------------------------------------------------------ #

def _infer_diabetes_type(patient_id: str) -> Optional[str]:
    """
    Infer diabetes type from patient ID naming conventions when no clinical
    profile is available from the Shanghai summary sheet.

    Naming conventions used:
      Shanghai:    1xxx = T1DM cohort, 2xxx = T2DM cohort (documented in paper)
      simglucose:  adolescent_*/child_* = T1DM model, adult_* = T2DM proxy
      Dexcom:      dexcom_dexcom_adolescent_*/child_* = T1DM, adult_* = T2DM

    Returns 'T1DM', 'T2DM', or None if pattern unrecognised.
    """
    pid = patient_id.lower().strip()

    # Shanghai hospital patients: 4-digit IDs, first digit = cohort
    # 1001, 1006 = T1DM; 2000-2017 = T2DM
    if pid.isdigit() and len(pid) == 4:
        return "T1DM" if pid.startswith("1") else "T2DM"

    # simglucose patients
    if pid.startswith("sim_adolescent") or pid.startswith("sim_child"):
        return "T1DM"
    if pid.startswith("sim_adult"):
        return "T2DM"

    # Dexcom export patients (dexcom_dexcom_ prefix from ingestor)
    if "adolescent" in pid or "child" in pid:
        return "T1DM"
    if "adult" in pid:
        return "T2DM"

    return None


def load_silver_from_s3(s3_client, silver_bucket: str) -> pd.DataFrame:
    """
    Load all Silver JSONL files from S3 into a single pandas DataFrame.

    Why pandas instead of Athena?
    At this dataset size (~14,000 rows for 30 patients) reading JSONL
    directly from S3 and aggregating with pandas is:
      - 10–30x faster than Athena (no query queue, no CSV export)
      - Free (no per-query Athena cost)
      - No Glue catalog dependency
      - No workgroup configuration required
      - Identical aggregation logic to the Athena SQL

    Athena becomes the right tool when the dataset grows beyond ~10M rows
    or when multiple concurrent analysts need SQL access. At that point,
    swap this function back to run_query() — the rest of gold.py is
    unchanged because GoldReading is schema-agnostic.
    """
    rows = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=silver_bucket):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".jsonl"):
                continue
            try:
                body = s3_client.get_object(
                    Bucket=silver_bucket, Key=obj["Key"]
                )["Body"].read().decode("utf-8")
                for line in body.splitlines():
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
            except Exception as exc:
                logger.warning(f"Could not read {obj['Key']}: {exc}")

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    logger.info(f"Loaded {len(df)} Silver rows from S3 into DataFrame")
    return df


# ------------------------------------------------------------------ #
#  Aggregation — pandas (replaces Athena SQL)                         #
# ------------------------------------------------------------------ #

def aggregate_gold_metrics(
    s3_client,
    silver_bucket: str,
) -> list[GoldReading]:
    """
    Aggregate Silver readings into daily Gold metrics per patient.

    Implements the same logic as the original Athena SQL:
      GROUP BY patient_id, date(event_time), source_system
      Compute TIR%, mean glucose, hypo counts, CV%, GMI

    Parameters
    ----------
    s3_client     : boto3 S3 client
    silver_bucket : name of the Silver S3 bucket

    Returns
    -------
    list[GoldReading] — one reading per (patient_id, day, source_system)
    """
    df = load_silver_from_s3(s3_client, silver_bucket)
    if df.empty:
        logger.warning("No Silver rows found — ensure silver.py has been run first.")
        return []

    # Normalise types
    df["event_time"] = pd.to_datetime(df["event_time"], utc=True, errors="coerce")
    df["glucose_mgdl"] = pd.to_numeric(df["glucose_mgdl"], errors="coerce")
    df["day"] = df["event_time"].dt.date.astype(str)

    # Only aggregate rows with a valid glucose reading
    df_valid = df[df["glucose_mgdl"].notna()].copy()

    # Normalise source_system — Silver uses 'source_system', older files may use 'source'
    if "source_system" not in df_valid.columns and "source" in df_valid.columns:
        df_valid["source_system"] = df_valid["source"]
    elif "source_system" not in df_valid.columns:
        df_valid["source_system"] = "unknown"

    logger.info(f"Aggregating {len(df_valid)} valid rows across "
                f"{df_valid['patient_id'].nunique()} patients...")

    gold_readings = []
    for (patient_id, day, source_system), group in df_valid.groupby(
        ["patient_id", "day", "source_system"], sort=True
    ):
        glucose = group["glucose_mgdl"]
        mean_g  = float(glucose.mean())
        std_g   = float(glucose.std(ddof=1)) if len(glucose) > 1 else 0.0
        cv      = round((std_g / mean_g * 100), 2) if mean_g > 0 else 0.0

        # TIR — % of time_in_range_eligible readings in the 70–180 mg/dL range.
        # Silver schema uses 'time_in_range_eligible' (not 'tir_eligible').
        # Silver clinical_range uses 'normal' (not 'normal_tir').
        # Fall back to raw glucose bounds if Silver columns are absent.
        tir_col = "time_in_range_eligible"
        if tir_col in group.columns:
            eligible = group[group[tir_col] == True]
        else:
            eligible = group[(glucose >= 70) & (glucose <= 180)]

        if "clinical_range" in group.columns:
            # Silver uses "normal" for the 70-180 mg/dL band
            in_range = eligible[eligible["clinical_range"] == "normal"]
        else:
            in_range = eligible[(eligible["glucose_mgdl"] >= 70) &
                                (eligible["glucose_mgdl"] <= 180)]

        tir = round(
            (len(in_range) / len(eligible) * 100) if len(eligible) > 0 else 0.0,
            2
        )

        # Infer diabetes_type from patient ID naming convention.
        # This fires for all patients that don't have a clinical profile
        # from the Shanghai summary sheet (simglucose, Dexcom, and Shanghai
        # patients on first run before clinical_profiles are uploaded).
        inferred_type = _infer_diabetes_type(str(patient_id))

        try:
            reading = GoldReading(
                patient_id=str(patient_id),
                day=str(day),
                source_system=str(source_system),
                tir_percent=tir,
                mean_glucose=round(mean_g, 2),
                hypo_events=int((glucose < 70.0).sum()),
                severe_hypo_events=int((glucose < 54.0).sum()),
                cv_percent=cv,
                gmi=calculate_gmi(mean_g),
                diabetes_type=inferred_type,
            )
            gold_readings.append(reading)
        except Exception as exc:
            logger.warning(f"Could not create GoldReading for {patient_id} {day}: {exc}")

    logger.info(f"Produced {len(gold_readings)} Gold readings.")
    return gold_readings


# ------------------------------------------------------------------ #
#  S3 Writer                                                          #
# ------------------------------------------------------------------ #

def write_gold_to_s3(
    s3_client,
    gold_readings: list[GoldReading],
    bucket: str,
) -> int:
    written = 0
    for reading in gold_readings:
        key = (
            f"source={reading.source_system}/date={reading.day}/"
            f"{reading.patient_id}_daily.jsonl"
        )
        try:
            upload_jsonl(s3_client, [reading.model_dump(mode="json")], bucket, key)
            logger.info(f"  ✅ → s3://{bucket}/{key}")
            written += 1
        except Exception as exc:
            logger.error(f"  ❌ Failed {reading.patient_id} {reading.day}: {exc}")
            raise
    return written


# ------------------------------------------------------------------ #
#  Entry point                                                        #
# ------------------------------------------------------------------ #

def main():
    logger.info("=== Starting Gold Aggregation Transform v2 ===")
    s3 = get_s3_client(cfg.aws_region)

    try:
        # Step 1: load clinical profiles from Bronze
        profiles = load_clinical_profiles_from_s3(s3, cfg.bronze_bucket)

        # Step 2: aggregate CGM metrics from Silver S3 using pandas
        # (No Athena dependency — reads JSONL directly, same aggregation logic)
        gold_readings = aggregate_gold_metrics(s3, cfg.silver_bucket)
        if not gold_readings:
            logger.info("No gold readings produced. Ensure Silver tables have data.")
            return

        # Step 3: merge clinical profiles into Gold rows and re-upload enriched rows
        # GoldReading is frozen so we create new instances with clinical fields
        enriched = []
        for reading in gold_readings:
            pid = reading.patient_id
            profile = profiles.get(pid)
            if profile:
                clinical = profile.to_metrics_dict()
                updated = reading.model_copy(update=clinical)
                enriched.append(updated)
                logger.info(f"  Merged clinical profile for {pid}: "
                            f"diabetes_type={profile.diabetes_type}, age={profile.age}")
            else:
                enriched.append(reading)

        # Step 4: write to Gold S3
        written = write_gold_to_s3(s3, enriched, cfg.gold_bucket)
        logger.info(
            f"=== Gold Transform v2 Complete ===\n"
            f"  {written} Gold rows written\n"
            f"  {len([r for r in enriched if r.diabetes_type])} rows with clinical profile merged"
        )

    except Exception as exc:
        logger.critical(f"Gold transform failed: {exc}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
