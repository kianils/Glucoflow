"""
schema.py — GlucoFlow canonical reading model

Every ingestion path (Shanghai, simglucose, Dexcom-style messy) must produce
rows that conform to CanonicalReading before anything is written to S3.

Glucose flag rules (applied in from_raw):
  "Low"  string            → glucose_mgdl=None,  flag="low"
  "High" string            → glucose_mgdl=None,  flag="high"
  Non-numeric / missing    → glucose_mgdl=None,  flag="invalid"
  Numeric, outside 40–400  → glucose_mgdl=value, flag="out_of_range"
  Numeric, 40–400 mg/dL    → glucose_mgdl=value, flag="in_range"

These ranges are sensor-validity bounds, not clinical targets.
Clinical time-in-range (70–180) is a downstream analytics concern.
"""

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


GlucoseFlag = Literal["in_range", "low", "high", "invalid", "out_of_range"]
SourceSystem = Literal["shanghai", "simglucose", "dexcom_messy"]
BatchOrStream = Literal["batch", "stream"]

GLUCOSE_MIN = 40.0   # mg/dL — below this, sensors report "Low"
GLUCOSE_MAX = 400.0  # mg/dL — above this, sensors report "High"


class CanonicalReading(BaseModel):
    """Single CGM (or related) reading in the GlucoFlow canonical schema."""

    model_config = ConfigDict(frozen=True)

    # --- Identity ---
    patient_id: str = Field(..., description="Patient or subject identifier, source-scoped")
    lineage_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="UUID4 assigned at ingestion — used for deduplication and audit",
    )

    # --- Timing ---
    event_time: datetime = Field(
        ...,
        description="UTC timestamp of the reading as recorded by the source device/system",
    )
    ingest_time: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when this row was processed by GlucoFlow",
    )

    # --- Glucose ---
    glucose_mgdl: Optional[float] = Field(
        default=None,
        description="Glucose value in mg/dL; None when flag is low/high/invalid",
    )
    glucose_flag: GlucoseFlag = Field(
        ...,
        description="Validity/range classification of the glucose value",
    )

    # --- Provenance ---
    source_system: SourceSystem = Field(
        ..., description="Which upstream system this row originated from"
    )
    batch_or_stream: BatchOrStream = Field(
        ..., description="Whether this row arrived via batch upload or streaming ingest"
    )

    # --- Audit ---
    raw_payload: dict = Field(
        ...,
        description="Original source row, stored verbatim for traceability and replay",
    )

    # ------------------------------------------------------------------ #
    #  Factory                                                             #
    # ------------------------------------------------------------------ #

    @classmethod
    def from_raw(
        cls,
        row: dict,
        *,
        patient_id: str,
        event_time: datetime,
        source_system: SourceSystem,
        batch_or_stream: BatchOrStream,
        glucose_raw,  # the raw glucose cell — str | float | int | None
    ) -> "CanonicalReading":
        """
        Build a CanonicalReading from a raw source row.

        Parameters
        ----------
        row             : original row dict — stored verbatim in raw_payload
        patient_id      : caller supplies this (extracted from filename or column)
        event_time      : caller parses and normalises to UTC datetime
        source_system   : which of the three sources this came from
        batch_or_stream : ingestion mode
        glucose_raw     : the raw glucose cell before any cleaning
        """
        glucose_mgdl, glucose_flag = _classify_glucose(glucose_raw)

        # Ensure event_time is timezone-aware UTC
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)

        return cls(
            patient_id=patient_id,
            event_time=event_time,
            glucose_mgdl=glucose_mgdl,
            glucose_flag=glucose_flag,
            source_system=source_system,
            batch_or_stream=batch_or_stream,
            raw_payload=row,
        )


# ------------------------------------------------------------------ #
#  Internal helpers                                                    #
# ------------------------------------------------------------------ #

def _classify_glucose(raw) -> tuple[Optional[float], GlucoseFlag]:
    """
    Apply the glucose classification rules and return (value, flag).

    "Low"  string → (None, "low")
    "High" string → (None, "high")
    Missing/None  → (None, "invalid")
    Garbage str   → (None, "invalid")
    Numeric, 40–400   → (value, "in_range")
    Numeric, outside  → (value, "out_of_range")
    """
    if raw is None or (isinstance(raw, float) and _is_nan(raw)):
        return None, "invalid"

    if isinstance(raw, str):
        clean = raw.strip()
        if clean.lower() == "low":
            return None, "low"
        if clean.lower() == "high":
            return None, "high"
        if clean == "" or clean == "/":
            return None, "invalid"
        try:
            value = float(clean)
        except ValueError:
            return None, "invalid"
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None, "invalid"

    if GLUCOSE_MIN <= value <= GLUCOSE_MAX:
        return value, "in_range"
    else:
        return value, "out_of_range"


def _is_nan(v: float) -> bool:
    return v != v  # NaN is the only float not equal to itself
