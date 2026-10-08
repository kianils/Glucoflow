"""
protocol.py — GlucoFlow Clinical Trial Protocol Schema

Defines the data model for a clinical trial protocol's CGM-based
inclusion and exclusion criteria. Every criterion is a structured,
computable rule — not free text — so the eligibility engine can
evaluate it deterministically against a patient's Gold metrics.

Design Principles:
  - Protocols are defined as JSON files in src/trials/protocols/ and
    loaded via load_protocol(). This means new trials can be added
    without touching any Python code — just drop a JSON file.
  - Every criterion carries a human_rationale field so the AI layer
    can explain decisions in plain English, not just true/false.
  - The schema is intentionally minimal for v1 (CGM metrics only).
    Future iterations will add: demographics, comorbidities, lab
    values, concomitant medications — mapping to CDISC SDTM domains
    (DM, VS, LB, CM) for FDA-submission-grade traceability.

Iteration Path (documented here for future reference):
  v1 (current): CGM metrics only — TIR%, GMI, hypo events, CV%
  v2 (done):     + Clinical fields: age, diabetes type, duration,
                   retinopathy, nephropathy, neuropathy
  v3 (planned):  + CDISC SDTM field mapping for FDA traceability
  v4 (planned):  + EHR integration hooks (Epic FHIR R4 endpoints)
"""

import json
from pathlib import Path
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# ------------------------------------------------------------------ #
#  Criterion model                                                     #
# ------------------------------------------------------------------ #

# Supported comparison operators
Operator = Literal[">=", "<=", ">", "<", "==", "!="]

# CGM Gold-layer metric fields (v1)
CgmField = Literal[
    "tir_percent",
    "mean_glucose",
    "hypo_events",
    "severe_hypo_events",
    "cv_percent",
    "gmi",
]

# v2: Clinical / demographic fields from the Shanghai dataset and similar
# Numeric fields: age, duration_years — compared with threshold (float)
# Bool fields: has_retinopathy, has_nephropathy, has_neuropathy — encode
#   True as threshold=1.0, False as threshold=0.0 with operator "=="
# String field: diabetes_type — use threshold_str instead of threshold
ClinicalField = Literal[
    "diabetes_type",     # str: 'T1DM' or 'T2DM' — use threshold_str
    "age",               # int — compared as float
    "duration_years",    # float — years since diagnosis
    "has_retinopathy",   # bool — 1.0=True, 0.0=False with operator =="
    "has_nephropathy",   # bool — 1.0=True, 0.0=False with operator =="
    "has_neuropathy",    # bool — 1.0=True, 0.0=False with operator =="
]

# Union of both sets — the full criterion field type
ProtocolField = Union[CgmField, ClinicalField]


class TrialCriterion(BaseModel):
    """
    A single computable inclusion or exclusion criterion.

    Supports both CGM metrics (v1) and clinical/demographic fields (v2).

    For numeric and bool fields: supply ``threshold`` (float).
    For string fields (diabetes_type): supply ``threshold_str`` instead.
    Bool fields encode True as threshold=1.0 and False as threshold=0.0
    with operator "==".

    Example (CGM):
        TrialCriterion(
            field="tir_percent",
            operator="<=",
            threshold=70.0,
            human_label="TIR must be 70% or below",
            human_rationale="..."
        )

    Example (clinical string):
        TrialCriterion(
            field="diabetes_type",
            operator="==",
            threshold_str="T2DM",
            human_label="T2DM required",
            human_rationale="..."
        )
    """
    model_config = ConfigDict(frozen=True)

    field: Union[
    Literal["tir_percent","mean_glucose","hypo_events","severe_hypo_events","cv_percent","gmi"],
    Literal["diabetes_type","age","duration_years","has_retinopathy","has_nephropathy","has_neuropathy"]
    ]
    operator: Operator = Field(
        ...,
        description="Comparison operator: >=, <=, >, <, ==, !="
    )
    # Numeric threshold — used for all non-string fields
    threshold: Optional[float] = Field(
    default=None,
    description="Numeric threshold for CGM/demographic fields (float/int/bool→float)"
    )
    threshold_str: Optional[str] = Field(
        default=None,
        description="String threshold for categorical fields like diabetes_type"
    )
    human_label: str = Field(
        ...,
        description="Short human-readable label for this criterion (shown in reports)"
    )
    human_rationale: str = Field(
        ...,
        description=(
            "Clinical rationale explaining WHY this criterion exists. "
            "Surfaced by the AI layer when explaining inclusion/exclusion decisions."
        )
    )


# ------------------------------------------------------------------ #
#  Trial Protocol model                                               #
# ------------------------------------------------------------------ #

class TrialProtocol(BaseModel):
    """
    Full definition of a clinical trial's CGM-based eligibility criteria.

    Fields
    ------
    trial_id : str
        Short machine-readable identifier, e.g. "DIAMOND_T1DM_V1".
    trial_name : str
        Full human-readable name.
    phase : str
        Clinical trial phase: "I", "II", "III", "IV", or "RWE"
        (Real-World Evidence — observational studies also use this schema).
    sponsor : str
        Sponsoring organisation or research group.
    description : str
        One-paragraph plain-English description of what the trial is testing.
    inclusion_criteria : list[TrialCriterion]
        Patient MUST satisfy ALL of these to be eligible.
    exclusion_criteria : list[TrialCriterion]
        Patient must NOT satisfy ANY of these to be eligible.
    notes : str, optional
        Any additional protocol notes (e.g. "Based on Lancet 2017 DIAMOND trial").
    """
    model_config = ConfigDict(frozen=True)

    trial_id: str
    trial_name: str
    phase: str
    sponsor: str
    description: str
    inclusion_criteria: list[TrialCriterion] = Field(default_factory=list)
    exclusion_criteria: list[TrialCriterion] = Field(default_factory=list)
    notes: Optional[str] = None


# ------------------------------------------------------------------ #
#  Protocol loader                                                     #
# ------------------------------------------------------------------ #

PROTOCOLS_DIR = Path(__file__).parent / "protocols"


def load_protocol(trial_id: str) -> TrialProtocol:
    """
    Load a TrialProtocol from a JSON file in src/trials/protocols/.

    Parameters
    ----------
    trial_id : str
        Must match the filename exactly (without .json extension),
        e.g. "diamond_t1dm" loads diamond_t1dm.json.

    Returns
    -------
    TrialProtocol

    Raises
    ------
    FileNotFoundError if the protocol file does not exist.
    ValidationError if the JSON does not match the schema.
    """
    protocol_path = PROTOCOLS_DIR / f"{trial_id}.json"
    if not protocol_path.exists():
        available = [p.stem for p in PROTOCOLS_DIR.glob("*.json")]
        raise FileNotFoundError(
            f"Protocol '{trial_id}' not found. "
            f"Available protocols: {available}"
        )
    with open(protocol_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return TrialProtocol(**data)


def list_protocols() -> list[str]:
    """Return a list of all available trial protocol IDs."""
    return sorted(p.stem for p in PROTOCOLS_DIR.glob("*.json"))
