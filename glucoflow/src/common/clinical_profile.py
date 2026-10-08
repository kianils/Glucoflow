"""
clinical_profile.py — GlucoFlow v2 Clinical Profile Schema

Captures the non-CGM demographic and clinical fields extracted from the
Shanghai T1DM/T2DM dataset summary sheets. These fields enable the
eligibility engine to evaluate clinical criteria (diabetes type, age,
comorbidities) in addition to the CGM metrics already in Gold.

Data Source
-----------
The Shanghai dataset summary sheets contain one row per patient admission
with the following columns (among others):
  - Patient number, Age, Sex, BMI
  - Diabetes type (T1DM / T2DM), Duration (years since diagnosis)
  - HbA1c at admission (lab value, %)
  - Complications: retinopathy, nephropathy, neuropathy (Y/N)
  - Background medications: insulin, metformin, GLP-1, SGLT-2
  - Lab values: creatinine, eGFR, LDL, triglycerides

Design Principles
-----------------
- Optional fields throughout: not every patient admission has every field
  recorded — None means "not documented", not "absent"
- All bool fields (has_retinopathy etc) default to None so the eligibility
  engine can distinguish "confirmed absent" (False) from "not recorded" (None)
- Frozen (immutable): clinical profiles are facts about a patient at
  admission — they should not be mutated after extraction
- diabetes_type is a Literal to catch data quality issues at parse time
  (anything other than 'T1DM' or 'T2DM' raises ValidationError)

Iteration Path
--------------
v2 (current): Shanghai summary sheet fields
v3 (planned):  + FHIR R4 mapping (Patient / Condition / Observation resources)
v4 (planned):  + CDISC SDTM domain mapping (DM, VS, LB, CM)
"""

from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class ClinicalProfile(BaseModel):
    """
    Demographic and clinical metadata for one patient.

    Extracted from the Shanghai dataset summary sheet and merged into
    the Gold layer alongside CGM-derived metrics. All fields are Optional
    because real-world clinical datasets have missing values.

    Fields mirror the Shanghai summary sheet columns exactly so that
    the extraction code is a direct column-to-field mapping.

    Attributes
    ----------
    patient_id : str
        Same patient_id used throughout the pipeline — the join key
        between ClinicalProfile and GoldReading.

    diabetes_type : 'T1DM' | 'T2DM' | None
        From the 'Diabetes type' or 'Type' column. The most important
        field for trial screening — DIAMOND and closed-loop trials are
        T1DM-only; GLP-1 trials are T2DM-only.

    age : int | None
        Patient age at admission in years. Used by some protocols
        (e.g. DIAMOND requires age ≥ 25).

    sex : 'M' | 'F' | None
        Biological sex. Not currently used in eligibility criteria but
        required for population-level reporting and regulatory submissions.

    bmi : float | None
        Body mass index (kg/m²). GLP-1 trials often have BMI thresholds
        (e.g. BMI ≥ 25 for T2DM overweight population).

    diabetes_duration_years : float | None
        Years since diabetes diagnosis. DIAMOND required duration ≥ 1 year
        to ensure the patient's insulin regimen was established.

    hba1c_admission : float | None
        Laboratory-measured HbA1c (%) at hospital admission. This is the
        gold-standard glycaemic control metric. GMI (from CGM) is an
        estimate of HbA1c; having the actual lab value in v2 means
        we can use the real number instead of the estimate.

    has_retinopathy : bool | None
        Diabetic retinopathy documented in the clinical record.
        Retinopathy is a marker of long-standing poor control and is
        used as an exclusion in some cardiac safety trials.

    has_nephropathy : bool | None
        Diabetic nephropathy (kidney disease). Critical for GLP-1 trials —
        semaglutide (SUSTAIN-6) required eGFR ≥ 30 mL/min/1.73m²;
        nephropathy is a marker of reduced eGFR.

    has_neuropathy : bool | None
        Diabetic peripheral neuropathy. Less commonly used in eligibility
        criteria but relevant for APS trials (foot care / wound healing).

    on_insulin : bool | None
        Whether the patient was on insulin therapy at admission.
        DIAMOND required patients on multiple daily injections (MDI).
        Closed-loop trials require existing insulin therapy.

    on_metformin : bool | None
        Whether the patient was on metformin. GLP-1 trials (SUSTAIN/PIONEER)
        enrolled patients on metformin ± basal insulin background therapy.

    on_glp1 : bool | None
        Whether the patient was already on a GLP-1 receptor agonist.
        A patient already on GLP-1 would typically be excluded from a
        GLP-1 efficacy trial (already on the intervention).

    on_sglt2 : bool | None
        Whether the patient was on an SGLT-2 inhibitor. Increasingly
        used as background therapy in T2DM trials.

    egfr : float | None
        Estimated glomerular filtration rate (mL/min/1.73m²). The key
        renal function metric. GLP-1 trials require eGFR ≥ 30;
        SGLT-2 trials require eGFR ≥ 45.

    creatinine : float | None
        Serum creatinine (μmol/L). Used to compute eGFR. Stored for
        traceability — eGFR is the derived field used in criteria.

    ldl : float | None
        LDL cholesterol (mmol/L or mg/dL — units depend on source).
        Used in cardiovascular risk trials.

    triglycerides : float | None
        Serum triglycerides. Elevated in poorly-controlled T2DM.

    source_file : str | None
        The filename this profile was extracted from (e.g.
        'S1_patient_summary.xlsx'). For audit traceability.
    """

    model_config = ConfigDict(frozen=True)

    # ── Identity ──────────────────────────────────────────────────────
    patient_id: str = Field(
        ...,
        description="Join key to GoldReading — must match exactly"
    )

    # ── Diabetes Classification ───────────────────────────────────────
    diabetes_type: Optional[Literal["T1DM", "T2DM"]] = Field(
        default=None,
        description="T1DM or T2DM — the most critical field for trial matching"
    )
    diabetes_duration_years: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Years since diabetes diagnosis (≥ 0)"
    )
    hba1c_admission: Optional[float] = Field(
        default=None,
        ge=3.0,
        le=20.0,
        description="Lab-measured HbA1c at admission (%); range 3–20 guards against data errors"
    )

    # ── Demographics ──────────────────────────────────────────────────
    age: Optional[int] = Field(
        default=None,
        ge=0,
        le=120,
        description="Age at admission in years"
    )
    sex: Optional[Literal["M", "F"]] = Field(
        default=None,
        description="Biological sex — M or F"
    )
    bmi: Optional[float] = Field(
        default=None,
        ge=10.0,
        le=80.0,
        description="BMI (kg/m²); range guard 10–80"
    )

    # ── Complications ─────────────────────────────────────────────────
    has_retinopathy: Optional[bool] = Field(
        default=None,
        description="Diabetic retinopathy documented at admission"
    )
    has_nephropathy: Optional[bool] = Field(
        default=None,
        description="Diabetic nephropathy documented at admission"
    )
    has_neuropathy: Optional[bool] = Field(
        default=None,
        description="Diabetic peripheral neuropathy documented at admission"
    )

    # ── Medications ───────────────────────────────────────────────────
    on_insulin: Optional[bool] = Field(
        default=None,
        description="On insulin therapy at admission (MDI or pump)"
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

    # ── Lab Values ────────────────────────────────────────────────────
    egfr: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="eGFR (mL/min/1.73m²) — key renal function metric"
    )
    creatinine: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Serum creatinine (μmol/L)"
    )
    ldl: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="LDL cholesterol"
    )
    triglycerides: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Serum triglycerides"
    )

    # ── Provenance ────────────────────────────────────────────────────
    source_file: Optional[str] = Field(
        default=None,
        description="Source filename for audit traceability"
    )

    # ── Convenience helpers ───────────────────────────────────────────

    def to_metrics_dict(self) -> dict:
        """
        Return a flat dict of all non-None clinical fields, keyed by
        field name, suitable for merging into a Gold metrics dict before
        calling the eligibility engine.

        This is the bridge between ClinicalProfile and the eligibility
        engine's gold_metrics parameter. The eligibility engine calls
        gold_metrics.get(criterion.field) — so all clinical fields need
        to live in the same flat dict as the CGM metrics.

        Returns
        -------
        dict — only non-None fields are included so that the eligibility
               engine's missing-field handling is triggered correctly for
               fields that were genuinely not recorded.
        """
        raw = self.model_dump(mode="python", exclude={"patient_id", "source_file"})
        return {k: v for k, v in raw.items() if v is not None}

    @classmethod
    def unknown(cls, patient_id: str) -> "ClinicalProfile":
        """
        Return a minimal ClinicalProfile with all clinical fields as None.

        Used when a patient has CGM data in Gold but no corresponding
        summary sheet entry — the eligibility engine will treat all
        clinical criteria as 'metric unavailable'.
        """
        return cls(patient_id=patient_id)
