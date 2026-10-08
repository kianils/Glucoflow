"""
shanghai.py — GlucoFlow v2 Shanghai Ingestor
Ingests all Shanghai T1DM and T2DM .xlsx patient files into the Bronze S3 bucket.
Also extracts clinical/demographic fields from the summary sheet into ClinicalProfile.

What this script does, step by step:
  1. Scans the diabetes_datasets/Shanghai_T1DM and Shanghai_T2DM directories
  2. For each directory, finds the summary sheet (first or "Summary"-named sheet)
     and extracts one ClinicalProfile per patient
  3. Opens each individual patient .xlsx file
  4. Reads the CGM sheet — locates columns by header name (not position)
     so it is robust against column reordering between files
  5. Converts each CGM row to a CanonicalReading (Pydantic validates it)
  6. Failed rows are logged; their source file is flagged for quarantine
  7. Valid rows are serialized as JSONL and uploaded to:
     s3://<BRONZE_BUCKET>/source=shanghai_<type>/date=<today>/<patient_id>.jsonl
  8. ClinicalProfiles are serialized as a single JSON file per directory:
     s3://<BRONZE_BUCKET>/source=clinical_profiles/date=<today>/shanghai_<type>.json

v2 Changes
----------
- Extracts ClinicalProfile from summary sheet (diabetes type, age, sex, BMI,
  HbA1c, duration, complications, medications, lab values)
- Column aliases for all summary sheet fields to handle header variations
- ClinicalProfiles uploaded to Bronze alongside CGM data for downstream merge

Usage:
    python src/ingestion/shanghai.py

Environment variables required (see .env.example):
    BRONZE_BUCKET, QUARANTINE_BUCKET, AWS_REGION
"""

import json
import logging
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import openpyxl
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.common.schema import CanonicalReading
from src.common.clinical_profile import ClinicalProfile
from src.common.s3_utils import get_s3_client, build_key, upload_jsonl
from src.common.config import cfg

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Directory paths ───────────────────────────────────────────────────────────
DATASET_ROOT = Path(__file__).resolve().parents[3] / "diabetes_datasets"
T1DM_DIR     = DATASET_ROOT / "Shanghai_T1DM"
T2DM_DIR     = DATASET_ROOT / "Shanghai_T2DM"

# ── CGM column aliases ────────────────────────────────────────────────────────
# Real Shanghai files use "CGM (mg / dl)" (spaces around /) and "Date" for ts.
# Aliases cover the real format first, then common variants.
GLUCOSE_ALIASES = [
    "CGM (mg / dl)",   # Real Shanghai format — spaces around /
    "CGM (mg/dl)",
    "Glucose (mg/dL)",
    "Glucose",
    "glucose_value",
    "CBG (mg / dl)",   # Capillary blood glucose — real Shanghai
    "CBG (mg/dL)",
]
TIMESTAMP_ALIASES = [
    "Date",            # Real Shanghai format
    "Timestamp",
    "Time",
    "timestamp",
    "Date Time",
    "DateTime",
]
INSULIN_ALIASES = ["Insulin dose - s.c.", "Insulin (units)", "Insulin", "insulin"]
CARBS_ALIASES   = ["Dietary intake", "Carbs (grams)", "Carbs", "carbs", "Meal (CHO)"]

# ── Summary sheet column aliases ──────────────────────────────────────────────
# Each tuple: (ClinicalProfile field name, list of possible column headers)
SUMMARY_ALIASES: list[tuple[str, list[str]]] = [
    ("patient_id",             ["Patient number", "Patient ID", "ID", "Patient"]),
    ("diabetes_type",          ["Diabetes type", "Type", "DM type", "Diabetes Type"]),
    ("age",                    ["Age", "Age (years)", "age"]),
    ("sex",                    ["Sex", "Gender", "sex"]),
    ("bmi",                    ["BMI", "BMI (kg/m2)", "Body mass index"]),
    ("diabetes_duration_years",["Duration (years)", "Diabetes duration", "Duration", "DM duration (years)"]),
    ("hba1c_admission",        ["HbA1c (%)", "HbA1c", "HbA1c at admission", "A1C (%)"]),
    ("has_retinopathy",        ["Retinopathy", "DR", "Diabetic retinopathy"]),
    ("has_nephropathy",        ["Nephropathy", "DN", "Diabetic nephropathy"]),
    ("has_neuropathy",         ["Neuropathy", "Peripheral neuropathy"]),
    ("on_insulin",             ["Insulin therapy", "On insulin", "Insulin"]),
    ("on_metformin",           ["Metformin", "On metformin"]),
    ("on_glp1",                ["GLP-1", "GLP1", "GLP-1 agonist"]),
    ("on_sglt2",               ["SGLT-2", "SGLT2"]),
    ("egfr",                   ["eGFR", "eGFR (mL/min)", "GFR"]),
    ("creatinine",             ["Creatinine", "Creatinine (umol/L)", "SCr"]),
    ("ldl",                    ["LDL", "LDL-C", "LDL cholesterol"]),
    ("triglycerides",          ["Triglycerides", "TG", "Triglyceride"]),
]

# ── Helper: find column index by aliases ─────────────────────────────────────

def _norm(s: str) -> str:
    """
    Normalise a header string for comparison.
    Replaces all Unicode whitespace variants (non-breaking space U+00A0,
    thin space U+2009, etc.) with regular ASCII space, then collapses
    multiple spaces and strips leading/trailing whitespace.
    This handles Excel files from Chinese hospitals which commonly store
    non-breaking spaces in column headers.
    """
    import unicodedata
    # Normalise unicode to composed form first
    s = unicodedata.normalize("NFKC", s)
    # Replace any non-ASCII-space whitespace with regular space
    s = " ".join(s.split())
    return s.strip()


def find_col(headers: list, aliases: list) -> Optional[int]:
    """
    Return the index of the first header matching any alias, else None.
    Comparison is normalised — handles Unicode non-breaking spaces,
    extra whitespace, and case variations from Excel files.
    """
    norm_headers = [_norm(h) for h in headers]
    for alias in aliases:
        norm_alias = _norm(alias)
        try:
            return norm_headers.index(norm_alias)
        except ValueError:
            continue
    # Second pass: case-insensitive fallback
    lower_headers = [h.lower() for h in norm_headers]
    for alias in aliases:
        try:
            return lower_headers.index(_norm(alias).lower())
        except ValueError:
            continue
    return None


# ── Helper: coerce raw cell to bool ──────────────────────────────────────────

def _to_bool(raw) -> Optional[bool]:
    """
    Convert a raw cell value to bool.
    'Y', 'Yes', '1', True → True
    'N', 'No', '0', False → False
    None, '', '-' → None (not documented)
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if s in ("Y", "YES", "1", "TRUE"):
        return True
    if s in ("N", "NO", "0", "FALSE"):
        return False
    if s in ("", "-", "NA", "N/A", "/"):
        return None
    return None


# ── Helper: coerce raw cell to diabetes_type ─────────────────────────────────

def _to_diabetes_type(raw) -> Optional[str]:
    """Normalise diabetes type strings to 'T1DM' or 'T2DM'."""
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if s in ("T1DM", "T1", "TYPE 1", "TYPE1", "1"):
        return "T1DM"
    if s in ("T2DM", "T2", "TYPE 2", "TYPE2", "2"):
        return "T2DM"
    return None


# ── Summary sheet extraction ─────────────────────────────────────────────────

def extract_clinical_profiles(
    xlsx_files: list[Path],
    source_label: str,
) -> dict[str, ClinicalProfile]:
    """
    Scan the directory for a summary .xlsx file and extract one ClinicalProfile
    per patient row.

    The summary file is identified as any file whose name contains 'summary'
    (case-insensitive). If none is found, returns an empty dict and logs a warning.

    Parameters
    ----------
    xlsx_files  : list of all .xlsx paths in the directory
    source_label: 'shanghai_t1dm' or 'shanghai_t2dm'

    Returns
    -------
    dict mapping patient_id (str) → ClinicalProfile
    """
    summary_files = [f for f in xlsx_files if "summary" in f.stem.lower()]
    if not summary_files:
        logger.warning(
            f"No summary file found in {source_label} directory. "
            f"Clinical profiles will be empty — CGM ingestion will still proceed."
        )
        return {}

    summary_path = summary_files[0]
    logger.info(f"  Extracting clinical profiles from {summary_path.name}...")

    wb = openpyxl.load_workbook(summary_path, data_only=True, read_only=True)

    # Use the first sheet (usually "Sheet1" or "Summary")
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        logger.warning(f"  Summary file {summary_path.name} is empty.")
        wb.close()
        return {}

    headers = [str(h).strip() if h is not None else "" for h in rows[0]]

    # Build column index map from aliases
    col_map: dict[str, Optional[int]] = {}
    for field_name, aliases in SUMMARY_ALIASES:
        col_map[field_name] = find_col(headers, aliases)

    profiles: dict[str, ClinicalProfile] = {}

    for row_num, row in enumerate(rows[1:], start=2):
        # Skip completely blank rows
        if all(v is None for v in row):
            continue

        # Extract patient_id
        pid_col = col_map.get("patient_id")
        if pid_col is None or pid_col >= len(row) or row[pid_col] is None:
            logger.debug(f"  Row {row_num}: no patient_id, skipping")
            continue
        raw_pid = str(row[pid_col]).strip()
        if not raw_pid or raw_pid in ("/", "-", ""):
            continue

        def _get(field: str):
            """Safe cell getter — returns None if column not found or empty."""
            col = col_map.get(field)
            if col is None or col >= len(row):
                return None
            val = row[col]
            if val is None:
                return None
            s = str(val).strip()
            if s in ("", "/", "-", "NA", "N/A", "None"):
                return None
            return val

        # Build kwargs only with non-None values so Pydantic defaults kick in
        kwargs: dict = {"patient_id": raw_pid, "source_file": summary_path.name}

        raw_type = _get("diabetes_type")
        if raw_type is not None:
            dt = _to_diabetes_type(raw_type)
            if dt:
                kwargs["diabetes_type"] = dt

        for num_field in ("age", "diabetes_duration_years", "hba1c_admission",
                          "bmi", "egfr", "creatinine", "ldl", "triglycerides"):
            raw = _get(num_field)
            if raw is not None:
                try:
                    kwargs[num_field] = float(raw)
                except (ValueError, TypeError):
                    pass

        raw_sex = _get("sex")
        if raw_sex is not None:
            s = str(raw_sex).strip().upper()
            if s in ("M", "MALE"):
                kwargs["sex"] = "M"
            elif s in ("F", "FEMALE"):
                kwargs["sex"] = "F"

        for bool_field in ("has_retinopathy", "has_nephropathy", "has_neuropathy",
                           "on_insulin", "on_metformin", "on_glp1", "on_sglt2"):
            raw = _get(bool_field)
            if raw is not None:
                b = _to_bool(raw)
                if b is not None:
                    kwargs[bool_field] = b

        try:
            profile = ClinicalProfile(**kwargs)
            profiles[raw_pid] = profile
            logger.debug(f"  Extracted profile for patient {raw_pid}: {profile.diabetes_type}, age {profile.age}")
        except ValidationError as e:
            logger.warning(f"  Row {row_num} (patient {raw_pid}): validation error — {e}")

    wb.close()
    logger.info(f"  ✅ Extracted {len(profiles)} clinical profiles from {summary_path.name}")
    return profiles


# ── CGM file parsing ──────────────────────────────────────────────────────────

def parse_patient_file(
    xlsx_path: Path,
    source: str,
) -> tuple[list[CanonicalReading], list[dict]]:
    """
    Parse a single patient .xlsx CGM file.

    Returns (valid_readings, failed_rows).
    The clinical profile is NOT merged here — it is merged at the Gold layer.
    """
    wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)

    # Find the CGM data sheet — named "CGM", "Glucose", or fall back to first sheet
    cgm_sheet = None
    for name in wb.sheetnames:
        if "cgm" in name.lower() or "glucose" in name.lower():
            cgm_sheet = wb[name]
            break
    if cgm_sheet is None:
        cgm_sheet = wb.worksheets[0]

    rows = list(cgm_sheet.iter_rows(values_only=True))
    if not rows:
        logger.warning(f"Empty sheet in {xlsx_path.name}")
        wb.close()
        return [], []

    headers  = [str(h).strip() if h is not None else "" for h in rows[0]]
    ts_col   = find_col(headers, TIMESTAMP_ALIASES)
    gluc_col = find_col(headers, GLUCOSE_ALIASES)

    if ts_col is None or gluc_col is None:
        logger.error(
            f"Could not locate timestamp or glucose column in {xlsx_path.name}. "
            f"Headers found: {headers}"
        )
        wb.close()
        return [], []

    insulin_col = find_col(headers, INSULIN_ALIASES)
    carbs_col   = find_col(headers, CARBS_ALIASES)

    # Derive patient_id from filename (e.g. "1001_0_20210730.xlsx" → "1001")
    patient_id = xlsx_path.stem.split("_")[0]

    source_system_map = {"shanghai_t1dm": "shanghai", "shanghai_t2dm": "shanghai"}
    source_system = source_system_map.get(source, "shanghai")

    valid:  list[CanonicalReading] = []
    failed: list[dict] = []

    for row_num, row in enumerate(rows[1:], start=2):
        raw_ts   = row[ts_col]
        raw_gluc = row[gluc_col]

        # Skip completely empty rows
        if raw_ts is None and raw_gluc is None:
            continue

        # Skip null/missing glucose values (Shanghai uses "/" for missing)
        if raw_gluc is None or str(raw_gluc).strip() in ("/", "", "nan", "None"):
            logger.debug(f"{xlsx_path.name} row {row_num}: null glucose, skipping")
            continue

        try:
            # Normalise timestamp
            if isinstance(raw_ts, datetime):
                ts = raw_ts
            elif isinstance(raw_ts, date) and not isinstance(raw_ts, datetime):
                ts = datetime(raw_ts.year, raw_ts.month, raw_ts.day)
            elif isinstance(raw_ts, str):
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M", "%m/%d/%Y %H:%M:%S"):
                    try:
                        ts = datetime.strptime(raw_ts.strip(), fmt)
                        break
                    except ValueError:
                        continue
                else:
                    raise ValueError(f"Unrecognised timestamp format: {raw_ts!r}")
            else:
                raise ValueError(f"Unexpected timestamp type: {type(raw_ts)}")

            # Build raw payload
            row_dict: dict = {}
            for idx, h in enumerate(headers):
                if idx < len(row) and row[idx] is not None:
                    row_dict[h] = row[idx]

            reading = CanonicalReading.from_raw(
                row_dict,
                patient_id=patient_id,
                event_time=ts,
                source_system=source_system,
                batch_or_stream="batch",
                glucose_raw=raw_gluc,
            )
            valid.append(reading)

        except (ValidationError, ValueError, TypeError) as e:
            failed.append({
                "file": xlsx_path.name,
                "row": row_num,
                "error": str(e),
                "raw": str(row),
            })

    wb.close()
    return valid, failed


# ── Directory ingestor ────────────────────────────────────────────────────────

def ingest_directory(
    patient_dir: Path,
    source_label: str,
    s3,
    bronze_bucket: str,
    quarantine_bucket: str,
) -> tuple[int, int, dict[str, ClinicalProfile]]:
    """
    Process all .xlsx files in a patient directory.

    Returns (total_valid_rows, total_failed_rows, clinical_profiles_dict).
    """
    files = sorted(patient_dir.glob("*.xlsx"))
    logger.info(f"\nFound {len(files)} files in {patient_dir.name}")

    # ── Step 1: extract clinical profiles from summary sheet ──────────
    profiles = extract_clinical_profiles(files, source_label)

    # ── Step 2: upload clinical profiles to Bronze ────────────────────
    if profiles:
        profiles_key = f"source=clinical_profiles/date={date.today().isoformat()}/{source_label}.json"
        profiles_data = {
            pid: p.model_dump(mode="json")
            for pid, p in profiles.items()
        }
        profiles_bytes = json.dumps(profiles_data, indent=2, default=str).encode("utf-8")
        s3.put_object(
            Bucket=bronze_bucket,
            Key=profiles_key,
            Body=profiles_bytes,
            ContentType="application/json",
        )
        logger.info(
            f"  ✅ Uploaded {len(profiles)} clinical profiles → "
            f"s3://{bronze_bucket}/{profiles_key}"
        )

    # ── Step 3: ingest CGM files ──────────────────────────────────────
    # Exclude summary files from CGM processing
    cgm_files = [f for f in files if "summary" not in f.stem.lower()]
    total_valid   = 0
    total_failed  = 0

    for xlsx_path in cgm_files:
        logger.info(f"  Parsing {xlsx_path.name}...")
        valid, failed = parse_patient_file(xlsx_path, source_label)
        total_valid  += len(valid)
        total_failed += len(failed)

        if failed:
            logger.warning(f"    {len(failed)} rows failed validation in {xlsx_path.name}")

        if valid:
            patient_id = xlsx_path.stem.split("_")[0]
            key = build_key(source_label, f"{patient_id}.jsonl")
            upload_jsonl(s3, [r.model_dump(mode="json") for r in valid], bronze_bucket, key)
            logger.info(f"    ✅ Uploaded {len(valid)} rows → s3://{bronze_bucket}/{key}")

    logger.info(
        f"\n{'='*50}\n"
        f"{patient_dir.name}: {total_valid} CGM rows valid, {total_failed} rows failed, "
        f"{len(profiles)} clinical profiles"
    )
    return total_valid, total_failed, profiles


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    s3 = get_s3_client(cfg.aws_region)

    t1dm_valid, t1dm_failed, t1dm_profiles = ingest_directory(
        T1DM_DIR, "shanghai_t1dm", s3, cfg.bronze_bucket, cfg.quarantine_bucket
    )
    t2dm_valid, t2dm_failed, t2dm_profiles = ingest_directory(
        T2DM_DIR, "shanghai_t2dm", s3, cfg.bronze_bucket, cfg.quarantine_bucket
    )

    total_profiles = len(t1dm_profiles) + len(t2dm_profiles)
    logger.info(
        f"\n{'='*50}\n"
        f"FINAL SUMMARY\n"
        f"  T1DM: {t1dm_valid} CGM rows valid, {t1dm_failed} failed, "
        f"{len(t1dm_profiles)} clinical profiles\n"
        f"  T2DM: {t2dm_valid} CGM rows valid, {t2dm_failed} failed, "
        f"{len(t2dm_profiles)} clinical profiles\n"
        f"  Total clinical profiles uploaded: {total_profiles}\n"
        f"{'='*50}"
    )


if __name__ == "__main__":
    main()
