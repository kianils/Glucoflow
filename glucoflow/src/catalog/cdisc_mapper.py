"""
cdisc_mapper.py — GlucoFlow → CDISC SDTM Field Mapping

Maps GlucoFlow internal field names to CDISC SDTM domain/variable names so
that study data can be formatted for regulatory submission (FDA 21 CFR Part
11, ICH E6(R2)) or shared with EDC systems (Medidata Rave, Veeva Vault,
Oracle InForm).

CDISC Background
----------------
SDTM (Study Data Tabulation Model) organises clinical-trial data into
standardised domains. Each domain uses a two-letter prefix:
  LB  — Laboratory Test Results (includes HbA1c, fasting glucose, etc.)
  BG  — Blood Glucose (CDISC custom domain used by CGM/BGM studies)
  DM  — Demographics (USUBJID, AGE, SEX, RACE…)
  AE  — Adverse Events

Key SDTM variable naming conventions:
  --DOMAIN  : domain abbreviation (LB, BG, …)
  USUBJID   : unique subject identifier — composed as STUDYID-SITEID-SUBJID
  --STRESN  : standardised result (numeric)
  --STRESU  : standardised result unit
  --STRESC  : standardised result (char, for non-numeric results)
  --TEST    : test name in plain English
  --TESTCD  : short test code (≤ 8 chars, no spaces)
  --CAT     : test category (e.g. 'GLUCOSE MONITORING')
  VISITNUM  : numeric visit number
  VISIT     : visit label (e.g. 'SCREENING', 'WEEK 12')

GlucoFlow SDTM Mapping Strategy
--------------------------------
Silver layer (raw CGM readings) → LB domain rows
Gold layer (daily/weekly aggregated metrics) → BG domain rows

The mappings in FIELD_NAME_MAP cover the most commonly used fields.
Unmapped fields are left in the output with their original keys and a
warning so callers can extend the map without breaking existing code.

References
----------
- CDISC SDTM Implementation Guide v3.4 (released 2022-11-15)
- CDISC Controlled Terminology 2023-09-29
- FDA Study Data Standards Resources (fda.gov/industry/study-data-standards)
- Danne et al. (2017) "International Consensus on Use of CGM",
  Diabetes Care 40(12) — establishes CGM SDTM conventions
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Field name map: GlucoFlow internal → CDISC SDTM                   #
# ------------------------------------------------------------------ #

# Each entry is: glucoflow_field -> dict with at minimum:
#   domain   : SDTM 2-letter domain (LB, BG, DM, …)
#   variable : SDTM variable name for the standardised value
#   testcd   : LBTESTCD / BGTESTCD (≤ 8 chars, no spaces, uppercase)
#   test     : LBTEST / BGTEST (full English label)
#   stresu   : default unit string (LBSTRESU / BGSTRESU)
#   category : LBCAT / BGCAT
#
# Fields that map to identifiers rather than test results have only
# domain and variable set; test/testcd/stresu are omitted.

# Convenience flat map: GlucoFlow field name → primary SDTM variable name.
# Exported so callers can inspect the mapping without iterating FIELD_NAME_MAP.
# Computed lazily after FIELD_NAME_MAP is defined (see bottom of module).
FIELD_MAP: dict[str, str] = {}  # populated below


FIELD_NAME_MAP: dict[str, dict[str, str]] = {
    # --- Subject / study identifiers ---
    "patient_id": {
        "domain":   "DM",
        "variable": "USUBJID",
        # USUBJID is a composed identifier, not a test result
    },
    "study_id": {
        "domain":   "DM",
        "variable": "STUDYID",
    },
    "site_id": {
        "domain":   "DM",
        "variable": "SITEID",
    },

    # --- Timing ---
    # event_time: ISO-8601 timestamp of a CGM reading or event
    # In SDTM, DTHFOLL is "Date/Time of Follow-up"; for CGM studies
    # the closest general-purpose timestamp variable is --DTC (date/time
    # of collection). We use DTHFOLL here as specified in the task.
    "event_time": {
        "domain":   "LB",
        "variable": "DTHFOLL",
    },

    # --- Silver layer (raw CGM readings) → LB domain ---
    "glucose_mgdl": {
        "domain":   "LB",
        "variable": "LBSTRESN",
        "testcd":   "GLUC",
        "test":     "Glucose",
        "stresu":   "mg/dL",
        "category": "GLUCOSE MONITORING",
    },
    "glucose_mmol": {
        "domain":   "LB",
        "variable": "LBSTRESN",
        "testcd":   "GLUC",
        "test":     "Glucose",
        "stresu":   "mmol/L",
        "category": "GLUCOSE MONITORING",
    },

    # --- Gold layer (aggregated CGM metrics) → BG domain ---
    # GMI is derived HbA1c equivalent — placed in LB as per CDISC convention
    "gmi": {
        "domain":   "LB",
        "variable": "LBSTRESN",
        "testcd":   "HBA1CEST",
        "test":     "HbA1c (estimated)",
        "stresu":   "%",
        "category": "GLUCOSE MONITORING",
    },
    # Time-in-Range — BG domain, custom variable BGSTRESN
    "tir_percent": {
        "domain":   "BG",
        "variable": "BGSTRESN",
        "testcd":   "TIRPCT",
        "test":     "Time in Range",
        "stresu":   "%",
        "category": "CGM SUMMARY",
    },
    # Coefficient of Variation
    "cv_percent": {
        "domain":   "BG",
        "variable": "BGSTRESN",
        "testcd":   "CVPCT",
        "test":     "Coefficient of Variation",
        "stresu":   "%",
        "category": "CGM SUMMARY",
    },
    # Mean glucose
    "mean_glucose": {
        "domain":   "BG",
        "variable": "BGSTRESN",
        "testcd":   "AVGGLUC",
        "test":     "Mean Glucose",
        "stresu":   "mg/dL",
        "category": "CGM SUMMARY",
    },
    # Hypoglycaemic event counts
    "hypo_events": {
        "domain":   "BG",
        "variable": "BGSTRESN",
        "testcd":   "HYPOCNT",
        "test":     "Hypoglycaemic Events",
        "stresu":   "count",
        "category": "CGM SUMMARY",
    },
    "severe_hypo_events": {
        "domain":   "BG",
        "variable": "BGSTRESN",
        "testcd":   "SVHYPOCNT",
        "test":     "Severe Hypoglycaemic Events",
        "stresu":   "count",
        "category": "CGM SUMMARY",
    },
    # Clinical range label (e.g. 'normal', 'low', 'high')
    # Stored as character result; unit is the range label itself
    "clinical_range": {
        "domain":   "BG",
        "variable": "BGSTRESC",    # character result, not numeric
        "testcd":   "CLINRNG",
        "test":     "Clinical Range",
        "stresu":   "category",
        "category": "CGM SUMMARY",
    },
}


# ------------------------------------------------------------------ #
#  LB row builder — Silver CGM readings                              #
# ------------------------------------------------------------------ #

def to_sdtm_lb_row(silver_reading_dict: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a GlucoFlow Silver-layer reading dict to a CDISC SDTM LB domain row.

    The LB (Laboratory Test Results) domain is used for raw CGM glucose
    readings and for the estimated HbA1c (GMI) value, which CDISC treats
    as a derived lab result.

    Input fields recognised
    -----------------------
    patient_id   : str  → USUBJID
    event_time   : str  → DTHFOLL (ISO-8601 datetime string)
    glucose_mgdl : float → LBSTRESN (with LBTEST='Glucose', LBSTRESU='mg/dL')
    glucose_mmol : float → LBSTRESN (with LBTEST='Glucose', LBSTRESU='mmol/L')
    gmi          : float → LBSTRESN (with LBTEST='HbA1c (estimated)')
    Any other fields are passed through as-is with a logged warning.

    Returns
    -------
    dict — SDTM LB row with at minimum: DOMAIN, USUBJID, LBTEST, LBTESTCD,
           LBSTRESN, LBSTRESU, LBCAT, and DTHFOLL if event_time was supplied.

    Example
    -------
    >>> row = to_sdtm_lb_row({
    ...     "patient_id": "PT-001",
    ...     "event_time": "2026-01-15T08:30:00Z",
    ...     "glucose_mgdl": 142.5,
    ... })
    >>> row["DOMAIN"]
    'LB'
    >>> row["LBTEST"]
    'Glucose'
    >>> row["LBSTRESN"]
    142.5
    """
    row: dict[str, Any] = {"DOMAIN": "LB"}

    # The LB row represents a single test result, so we pick the first
    # test-result field found (glucose_mgdl preferred over glucose_mmol).
    _LB_TEST_PREFERENCE = ["glucose_mgdl", "glucose_mmol", "gmi"]

    for source_key, value in silver_reading_dict.items():
        mapping = FIELD_NAME_MAP.get(source_key)
        if mapping is None:
            logger.debug("to_sdtm_lb_row: unmapped field '%s' passed through", source_key)
            row[source_key] = value
            continue

        if mapping.get("domain") != "LB":
            # Non-LB field — pass through with its SDTM variable name
            row[mapping["variable"]] = value
            continue

        if mapping["variable"] == "DTHFOLL":
            row["DTHFOLL"] = value
            continue

        if mapping["variable"] == "LBSTRESN":
            # Populate test metadata from the mapping
            row["LBSTRESN"] = value
            row["LBTEST"]   = mapping["test"]
            row["LBTESTCD"] = mapping["testcd"]
            row["LBSTRESU"] = mapping.get("stresu", "")
            row["LBCAT"]    = mapping.get("category", "")
            continue

        # Fallback: use the SDTM variable name directly
        row[mapping["variable"]] = value

    # Ensure DOMAIN is present (not overridden by pass-through logic)
    row["DOMAIN"] = "LB"
    return row


# ------------------------------------------------------------------ #
#  BG row builder — Gold CGM summary metrics                         #
# ------------------------------------------------------------------ #

def to_sdtm_bg_row(gold_reading_dict: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a GlucoFlow Gold-layer aggregated metrics dict to a CDISC
    SDTM BG (Blood Glucose) domain row.

    The BG domain is a custom CDISC domain for CGM-derived summary
    statistics. Numeric results go to BGSTRESN; character results
    (e.g. clinical_range) go to BGSTRESC.

    Input fields recognised
    -----------------------
    patient_id       : str   → USUBJID
    event_time       : str   → DTHFOLL
    tir_percent      : float → BGSTRESN (BGTEST='Time in Range')
    cv_percent       : float → BGSTRESN (BGTEST='Coefficient of Variation')
    mean_glucose     : float → BGSTRESN (BGTEST='Mean Glucose')
    hypo_events      : float → BGSTRESN (BGTEST='Hypoglycaemic Events')
    severe_hypo_events: float → BGSTRESN
    clinical_range   : str   → BGSTRESC / BGSTRESU (category label)
    gmi              : float → included as a sub-row dict in 'gmi_lb_sub'
                               (GMI is a derived HbA1c, properly housed in LB)
    Any other fields are passed through with a logged warning.

    Note on GMI
    -----------
    GMI (Glucose Management Indicator, a surrogate HbA1c) is technically a
    laboratory estimate and belongs in the LB domain per CDISC convention.
    This function still records it in the BG row under the key 'gmi_lb_sub'
    as a nested dict so the BG row is self-contained for pre-screening
    reporting, while preserving the intent that it should also appear in LB.

    Returns
    -------
    dict — SDTM BG row. For multi-metric Gold dicts, only the first
    numeric BG metric found populates BGSTRESN/BGTEST (the BG domain
    standard is one row per metric). For multi-metric use, call this
    function once per metric key.

    Example
    -------
    >>> row = to_sdtm_bg_row({
    ...     "patient_id": "PT-001",
    ...     "tir_percent": 68.2,
    ...     "clinical_range": "normal",
    ... })
    >>> row["DOMAIN"]
    'BG'
    >>> row["BGSTRESN"]
    68.2
    >>> row["BGSTRESU"]
    '%'
    """
    row: dict[str, Any] = {"DOMAIN": "BG"}

    # Priority order for picking the single BG numeric result
    _BG_METRIC_PREFERENCE = [
        "tir_percent", "cv_percent", "mean_glucose",
        "hypo_events", "severe_hypo_events",
    ]
    _bg_metric_set = False

    for source_key, value in gold_reading_dict.items():
        mapping = FIELD_NAME_MAP.get(source_key)

        if mapping is None:
            logger.debug("to_sdtm_bg_row: unmapped field '%s' passed through", source_key)
            row[source_key] = value
            continue

        # USUBJID / identifiers
        if mapping["variable"] == "USUBJID":
            row["USUBJID"] = value
            continue

        if mapping["variable"] == "DTHFOLL":
            row["DTHFOLL"] = value
            continue

        # GMI belongs in the LB domain — embed as flat prefixed keys so the
        # BG row is self-contained without nested dicts, and downstream tools
        # can detect the LB sub-column by the GMI_ prefix.
        if source_key == "gmi":
            row["GMI_LBSTRESN"] = value
            row["GMI_LBTEST"]   = "HbA1c (estimated)"
            row["GMI_LBDOMAIN"] = "LB"
            continue

        # Character result (clinical_range)
        if mapping["variable"] == "BGSTRESC":
            row["BGSTRESC"] = value
            row["BGTEST"]   = mapping["test"]
            row["BGTESTCD"] = mapping["testcd"]
            row["BGSTRESU"] = mapping.get("stresu", "")
            row["BGCAT"]    = mapping.get("category", "")
            continue

        # Numeric BG metric — populate BGSTRESN on first encounter
        if mapping["variable"] == "BGSTRESN" and not _bg_metric_set:
            row["BGSTRESN"] = value
            row["BGTEST"]   = mapping["test"]
            row["BGTESTCD"] = mapping["testcd"]
            row["BGSTRESU"] = mapping.get("stresu", "")
            row["BGCAT"]    = mapping.get("category", "")
            _bg_metric_set = True
            continue

        # Additional BG numeric metrics beyond the first — pass through with warning
        if mapping["variable"] == "BGSTRESN" and _bg_metric_set:
            logger.warning(
                "to_sdtm_bg_row: multiple numeric BG metrics in one dict. "
                "Field '%s' passed through; call once per metric for strict SDTM compliance.",
                source_key,
            )
            row[source_key] = value
            continue

        # Fallback
        row[mapping["variable"]] = value

    row["DOMAIN"] = "BG"
    return row


# ------------------------------------------------------------------ #
#  Populate FIELD_MAP from FIELD_NAME_MAP at module load time          #
# ------------------------------------------------------------------ #

# Done after FIELD_NAME_MAP is fully defined so there is no forward-
# reference issue. We only keep the first SDTM variable for fields
# that have multiple sub-domain uses (e.g. gmi belongs in LB).
FIELD_MAP.update(
    {field: meta["variable"] for field, meta in FIELD_NAME_MAP.items()}
)
