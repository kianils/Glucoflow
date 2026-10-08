"""
test_v2_features.py — Test suite for all v2 additions

Coverage:
  TestClinicalFieldCriterion  : TrialCriterion with clinical fields (string + bool + numeric)
  TestClinicalEligibility     : evaluate_criterion and screen_patient with clinical fields
  TestNewProtocolsLoad        : declare_t2dm and sensor_augmented_pump JSON load + schema
  TestDeclareTrial            : end-to-end eligibility scenarios for DECLARE-TIMI/CANVAS model
  TestSapTrial                : end-to-end eligibility scenarios for SAP T1DM trial
  TestCdiscMapper             : to_sdtm_lb_row and to_sdtm_bg_row
  TestPdfReport               : generate_prescreening_report returns correct structured text
"""

import sys
from pathlib import Path
from datetime import date

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ------------------------------------------------------------------ #
#  TASK 1 — Clinical field expansion                                  #
# ------------------------------------------------------------------ #

class TestClinicalFieldCriterion:
    """TrialCriterion must accept clinical fields alongside CGM fields."""

    def test_diabetes_type_criterion_t2dm(self):
        """String criterion: diabetes_type == 'T2DM' must be constructable."""
        from src.trials.protocol import TrialCriterion
        c = TrialCriterion(
            field="diabetes_type",
            operator="==",
            threshold_str="T2DM",
            human_label="T2DM required",
            human_rationale="DECLARE-TIMI enrolled T2DM patients only.",
        )
        assert c.field == "diabetes_type"
        assert c.threshold_str == "T2DM"

    def test_age_criterion_numeric(self):
        """Numeric clinical criterion: age >= 18."""
        from src.trials.protocol import TrialCriterion
        c = TrialCriterion(
            field="age",
            operator=">=",
            threshold=18.0,
            human_label="Age ≥ 18",
            human_rationale="Adult patients only.",
        )
        assert c.threshold == 18.0

    def test_bool_criterion_has_nephropathy(self):
        """Bool exclusion field: has_nephropathy == True triggers exclusion."""
        from src.trials.protocol import TrialCriterion
        # Encoded as threshold=1.0 for True (True == 1.0 numerically)
        c = TrialCriterion(
            field="has_nephropathy",
            operator="==",
            threshold=1.0,
            human_label="Has severe nephropathy",
            human_rationale="Severe nephropathy exclusion per DECLARE-TIMI criteria.",
        )
        assert c.field == "has_nephropathy"

    def test_duration_years_criterion(self):
        """Numeric clinical criterion: duration_years >= 2."""
        from src.trials.protocol import TrialCriterion
        c = TrialCriterion(
            field="duration_years",
            operator=">=",
            threshold=2.0,
            human_label="Diabetes duration ≥ 2 years",
            human_rationale="DECLARE required established diabetes.",
        )
        assert c.threshold == 2.0

    def test_has_retinopathy_criterion(self):
        from src.trials.protocol import TrialCriterion
        c = TrialCriterion(
            field="has_retinopathy",
            operator="==",
            threshold=1.0,
            human_label="Has retinopathy",
            human_rationale="Retinopathy exclusion.",
        )
        assert c.field == "has_retinopathy"

    def test_has_neuropathy_criterion(self):
        from src.trials.protocol import TrialCriterion
        c = TrialCriterion(
            field="has_neuropathy",
            operator="==",
            threshold=1.0,
            human_label="Has neuropathy",
            human_rationale="Neuropathy exclusion.",
        )
        assert c.field == "has_neuropathy"

    def test_existing_cgm_criterion_still_works(self):
        """Backward compat: CGM-only criteria must still construct without threshold_str."""
        from src.trials.protocol import TrialCriterion
        c = TrialCriterion(
            field="tir_percent",
            operator="<=",
            threshold=70.0,
            human_label="TIR ≤ 70%",
            human_rationale="Standard CGM criterion.",
        )
        assert c.threshold == 70.0
        assert c.threshold_str is None


class TestClinicalEligibility:
    """evaluate_criterion must handle string and bool clinical fields."""

    def test_string_match_passes(self):
        from src.trials.protocol import TrialCriterion
        from src.trials.eligibility import evaluate_criterion
        c = TrialCriterion(
            field="diabetes_type",
            operator="==",
            threshold_str="T2DM",
            human_label="T2DM required",
            human_rationale="DECLARE-TIMI T2DM only.",
        )
        result = evaluate_criterion(c, {"diabetes_type": "T2DM"})
        assert result.passed is True

    def test_string_mismatch_fails(self):
        from src.trials.protocol import TrialCriterion
        from src.trials.eligibility import evaluate_criterion
        c = TrialCriterion(
            field="diabetes_type",
            operator="==",
            threshold_str="T2DM",
            human_label="T2DM required",
            human_rationale="DECLARE-TIMI T2DM only.",
        )
        result = evaluate_criterion(c, {"diabetes_type": "T1DM"})
        assert result.passed is False

    def test_bool_true_passes_equals_one(self):
        """has_nephropathy=True, threshold=1.0, op== → passes (triggers exclusion)."""
        from src.trials.protocol import TrialCriterion
        from src.trials.eligibility import evaluate_criterion
        c = TrialCriterion(
            field="has_nephropathy",
            operator="==",
            threshold=1.0,
            human_label="Has severe nephropathy",
            human_rationale="Nephropathy exclusion.",
        )
        result = evaluate_criterion(c, {"has_nephropathy": True})
        assert result.passed is True  # patient has nephropathy → exclusion triggered

    def test_bool_false_does_not_trigger_exclusion(self):
        """has_nephropathy=False, threshold=1.0, op== → does not pass (exclusion not triggered)."""
        from src.trials.protocol import TrialCriterion
        from src.trials.eligibility import evaluate_criterion
        c = TrialCriterion(
            field="has_nephropathy",
            operator="==",
            threshold=1.0,
            human_label="Has severe nephropathy",
            human_rationale="Nephropathy exclusion.",
        )
        result = evaluate_criterion(c, {"has_nephropathy": False})
        assert result.passed is False

    def test_age_numeric_passes(self):
        from src.trials.protocol import TrialCriterion
        from src.trials.eligibility import evaluate_criterion
        c = TrialCriterion(
            field="age",
            operator=">=",
            threshold=18.0,
            human_label="Age ≥ 18",
            human_rationale="Adults only.",
        )
        result = evaluate_criterion(c, {"age": 35})
        assert result.passed is True

    def test_age_too_young_fails(self):
        from src.trials.protocol import TrialCriterion
        from src.trials.eligibility import evaluate_criterion
        c = TrialCriterion(
            field="age",
            operator=">=",
            threshold=18.0,
            human_label="Age ≥ 18",
            human_rationale="Adults only.",
        )
        result = evaluate_criterion(c, {"age": 16})
        assert result.passed is False

    def test_duration_years_passes(self):
        from src.trials.protocol import TrialCriterion
        from src.trials.eligibility import evaluate_criterion
        c = TrialCriterion(
            field="duration_years",
            operator=">=",
            threshold=2.0,
            human_label="Duration ≥ 2y",
            human_rationale="Established diabetes.",
        )
        result = evaluate_criterion(c, {"duration_years": 5.5})
        assert result.passed is True

    def test_missing_clinical_field_fails(self):
        from src.trials.protocol import TrialCriterion
        from src.trials.eligibility import evaluate_criterion
        c = TrialCriterion(
            field="diabetes_type",
            operator="==",
            threshold_str="T2DM",
            human_label="T2DM required",
            human_rationale="T2DM only.",
        )
        result = evaluate_criterion(c, {})
        assert result.passed is False
        assert result.patient_value is None


# ------------------------------------------------------------------ #
#  TASK 2 — New protocol JSON files                                   #
# ------------------------------------------------------------------ #

class TestNewProtocolsLoad:
    """Both new protocol JSON files must load without validation errors."""

    def test_declare_t2dm_loads(self):
        from src.trials.protocol import load_protocol, TrialProtocol
        p = load_protocol("declare_t2dm")
        assert isinstance(p, TrialProtocol)
        assert p.trial_id == "declare_t2dm"

    def test_sensor_augmented_pump_loads(self):
        from src.trials.protocol import load_protocol, TrialProtocol
        p = load_protocol("sensor_augmented_pump")
        assert isinstance(p, TrialProtocol)
        assert p.trial_id == "sensor_augmented_pump"

    def test_list_protocols_now_has_five(self):
        from src.trials.protocol import list_protocols
        protocols = list_protocols()
        assert "declare_t2dm" in protocols
        assert "sensor_augmented_pump" in protocols
        # Still has the three originals
        assert "diamond_t1dm" in protocols
        assert "glp1_t2dm" in protocols
        assert "closed_loop_candidate" in protocols

    def test_declare_has_t2dm_criterion(self):
        from src.trials.protocol import load_protocol
        p = load_protocol("declare_t2dm")
        fields = {c.field for c in p.inclusion_criteria + p.exclusion_criteria}
        assert "diabetes_type" in fields

    def test_declare_has_duration_criterion(self):
        from src.trials.protocol import load_protocol
        p = load_protocol("declare_t2dm")
        fields = {c.field for c in p.inclusion_criteria}
        assert "duration_years" in fields

    def test_declare_has_gmi_range(self):
        from src.trials.protocol import load_protocol
        p = load_protocol("declare_t2dm")
        gmi_inc = [c for c in p.inclusion_criteria if c.field == "gmi"]
        assert len(gmi_inc) == 2  # min and max bound

    def test_declare_has_nephropathy_exclusion(self):
        from src.trials.protocol import load_protocol
        p = load_protocol("declare_t2dm")
        excl_fields = {c.field for c in p.exclusion_criteria}
        assert "has_nephropathy" in excl_fields

    def test_sap_has_t1dm_criterion(self):
        from src.trials.protocol import load_protocol
        p = load_protocol("sensor_augmented_pump")
        fields = {c.field for c in p.inclusion_criteria}
        assert "diabetes_type" in fields

    def test_sap_has_cv_percent_criterion(self):
        from src.trials.protocol import load_protocol
        p = load_protocol("sensor_augmented_pump")
        fields = {c.field for c in p.inclusion_criteria}
        assert "cv_percent" in fields

    def test_sap_has_age_range(self):
        from src.trials.protocol import load_protocol
        p = load_protocol("sensor_augmented_pump")
        age_inc = [c for c in p.inclusion_criteria if c.field == "age"]
        assert len(age_inc) == 2  # 18–65

    def test_sap_has_duration_criterion(self):
        from src.trials.protocol import load_protocol
        p = load_protocol("sensor_augmented_pump")
        inc_fields = {c.field for c in p.inclusion_criteria}
        assert "duration_years" in inc_fields

    def test_all_new_criteria_have_rationale(self):
        from src.trials.protocol import load_protocol
        for pid in ("declare_t2dm", "sensor_augmented_pump"):
            p = load_protocol(pid)
            for c in p.inclusion_criteria + p.exclusion_criteria:
                assert len(c.human_rationale) > 10, (
                    f"Protocol {pid}: criterion '{c.human_label}' has empty rationale"
                )


class TestDeclareTrial:
    """End-to-end DECLARE-TIMI/CANVAS-modelled eligibility scenarios."""

    def _eligible_patient(self) -> dict:
        return {
            "patient_id":      "t2dm_candidate",
            "diabetes_type":   "T2DM",
            "age":             58,
            "duration_years":  7.5,
            "has_nephropathy": False,
            "gmi":             8.1,          # 7.0–10.5 ✅
            "tir_percent":     48.0,
            "mean_glucose":    180.0,
            "hypo_events":     0,
            "severe_hypo_events": 0,
            "cv_percent":      24.0,
        }

    def _t1dm_patient(self) -> dict:
        """T1DM patient — fails diabetes_type == T2DM."""
        p = self._eligible_patient()
        p["diabetes_type"] = "T1DM"
        p["patient_id"] = "t1dm_wrong_type"
        return p

    def _short_duration_patient(self) -> dict:
        p = self._eligible_patient()
        p["duration_years"] = 0.5   # < 2 years ❌
        p["patient_id"] = "new_diagnosis"
        return p

    def _nephropathy_patient(self) -> dict:
        p = self._eligible_patient()
        p["has_nephropathy"] = True   # triggers exclusion ❌
        p["patient_id"] = "nephropathy_patient"
        return p

    def test_eligible_t2dm_patient(self):
        from src.trials.protocol import load_protocol
        from src.trials.eligibility import screen_patient
        p = load_protocol("declare_t2dm")
        result = screen_patient("t2dm_candidate", self._eligible_patient(), p)
        assert result.eligible is True

    def test_t1dm_patient_not_eligible(self):
        from src.trials.protocol import load_protocol
        from src.trials.eligibility import screen_patient
        p = load_protocol("declare_t2dm")
        result = screen_patient("t1dm_wrong_type", self._t1dm_patient(), p)
        assert result.eligible is False

    def test_short_duration_not_eligible(self):
        from src.trials.protocol import load_protocol
        from src.trials.eligibility import screen_patient
        p = load_protocol("declare_t2dm")
        result = screen_patient("new_diagnosis", self._short_duration_patient(), p)
        assert result.eligible is False

    def test_nephropathy_patient_excluded(self):
        from src.trials.protocol import load_protocol
        from src.trials.eligibility import screen_patient
        p = load_protocol("declare_t2dm")
        result = screen_patient("nephropathy_patient", self._nephropathy_patient(), p)
        assert result.eligible is False
        excl_fields = {r.criterion.field for r in result.exclusion_triggered}
        assert "has_nephropathy" in excl_fields


class TestSapTrial:
    """End-to-end SAP T1DM trial eligibility scenarios."""

    def _eligible_patient(self) -> dict:
        return {
            "patient_id":      "sap_candidate",
            "diabetes_type":   "T1DM",
            "age":             34,
            "duration_years":  8.0,
            "has_nephropathy": False,
            "cv_percent":      40.0,   # ≥ 36% ✅
            "gmi":             8.5,
            "tir_percent":     52.0,
            "mean_glucose":    185.0,
            "hypo_events":     2,
            "severe_hypo_events": 0,
        }

    def _t2dm_patient(self) -> dict:
        p = self._eligible_patient()
        p["diabetes_type"] = "T2DM"
        p["patient_id"] = "t2dm_sap"
        return p

    def _too_old_patient(self) -> dict:
        p = self._eligible_patient()
        p["age"] = 70     # > 65 ❌
        p["patient_id"] = "elderly_sap"
        return p

    def _low_variability_patient(self) -> dict:
        p = self._eligible_patient()
        p["cv_percent"] = 22.0   # < 36% ❌
        p["patient_id"] = "stable_sap"
        return p

    def test_eligible_t1dm_patient(self):
        from src.trials.protocol import load_protocol
        from src.trials.eligibility import screen_patient
        p = load_protocol("sensor_augmented_pump")
        result = screen_patient("sap_candidate", self._eligible_patient(), p)
        assert result.eligible is True

    def test_t2dm_not_eligible_for_sap(self):
        from src.trials.protocol import load_protocol
        from src.trials.eligibility import screen_patient
        p = load_protocol("sensor_augmented_pump")
        result = screen_patient("t2dm_sap", self._t2dm_patient(), p)
        assert result.eligible is False

    def test_too_old_not_eligible(self):
        from src.trials.protocol import load_protocol
        from src.trials.eligibility import screen_patient
        p = load_protocol("sensor_augmented_pump")
        result = screen_patient("elderly_sap", self._too_old_patient(), p)
        assert result.eligible is False

    def test_low_variability_not_eligible(self):
        from src.trials.protocol import load_protocol
        from src.trials.eligibility import screen_patient
        p = load_protocol("sensor_augmented_pump")
        result = screen_patient("stable_sap", self._low_variability_patient(), p)
        assert result.eligible is False


# ------------------------------------------------------------------ #
#  TASK 3 — CDISC SDTM mapper                                         #
# ------------------------------------------------------------------ #

class TestCdiscMapper:
    """to_sdtm_lb_row and to_sdtm_bg_row produce correct SDTM-shaped dicts."""

    def _silver_reading(self) -> dict:
        return {
            "patient_id":     "adult#001",
            "event_time":     "2026-10-01T08:30:00",
            "glucose_mgdl":   142.0,
            "clinical_range": "in_range",
            "source_system":  "dexcom",
            "lineage_id":     "abc123",
        }

    def _gold_reading(self) -> dict:
        return {
            "patient_id":   "adult#001",
            "day":          "2026-10-01",
            "gmi":          7.8,
            "tir_percent":  62.0,
            "mean_glucose": 172.0,
            "cv_percent":   28.5,
        }

    def test_lb_row_has_usubjid(self):
        from src.catalog.cdisc_mapper import to_sdtm_lb_row
        row = to_sdtm_lb_row(self._silver_reading())
        assert row["USUBJID"] == "adult#001"

    def test_lb_row_has_domain(self):
        from src.catalog.cdisc_mapper import to_sdtm_lb_row
        row = to_sdtm_lb_row(self._silver_reading())
        assert row["DOMAIN"] == "LB"

    def test_lb_row_lbstresn_is_glucose(self):
        from src.catalog.cdisc_mapper import to_sdtm_lb_row
        row = to_sdtm_lb_row(self._silver_reading())
        assert row["LBSTRESN"] == 142.0

    def test_lb_row_has_dthfoll(self):
        from src.catalog.cdisc_mapper import to_sdtm_lb_row
        row = to_sdtm_lb_row(self._silver_reading())
        assert row["DTHFOLL"] == "2026-10-01T08:30:00"

    def test_lb_row_lbtest_is_glucose(self):
        from src.catalog.cdisc_mapper import to_sdtm_lb_row
        row = to_sdtm_lb_row(self._silver_reading())
        assert "Glucose" in row["LBTEST"]

    def test_lb_row_lbstresu_units(self):
        from src.catalog.cdisc_mapper import to_sdtm_lb_row
        row = to_sdtm_lb_row(self._silver_reading())
        assert row["LBSTRESU"] == "mg/dL"

    def test_bg_row_has_usubjid(self):
        from src.catalog.cdisc_mapper import to_sdtm_bg_row
        row = to_sdtm_bg_row(self._gold_reading())
        assert row["USUBJID"] == "adult#001"

    def test_bg_row_has_domain(self):
        from src.catalog.cdisc_mapper import to_sdtm_bg_row
        row = to_sdtm_bg_row(self._gold_reading())
        assert row["DOMAIN"] == "BG"

    def test_bg_row_bgstresn_is_tir(self):
        from src.catalog.cdisc_mapper import to_sdtm_bg_row
        row = to_sdtm_bg_row(self._gold_reading())
        assert row["BGSTRESN"] == 62.0

    def test_bg_row_bgstresu_is_percent(self):
        from src.catalog.cdisc_mapper import to_sdtm_bg_row
        row = to_sdtm_bg_row(self._gold_reading())
        assert row["BGSTRESU"] == "%"

    def test_bg_row_gmi_in_lb_sub(self):
        """GMI should map to LBSTRESN in LB sub-domain with LBTEST HbA1c (estimated)."""
        from src.catalog.cdisc_mapper import to_sdtm_bg_row
        row = to_sdtm_bg_row(self._gold_reading())
        assert row["GMI_LBSTRESN"] == 7.8
        assert row["GMI_LBTEST"] == "HbA1c (estimated)"

    def test_field_name_map_is_exported(self):
        """FIELD_MAP constant must be importable and cover key mappings."""
        from src.catalog.cdisc_mapper import FIELD_MAP
        assert FIELD_MAP["patient_id"] == "USUBJID"
        assert FIELD_MAP["glucose_mgdl"] == "LBSTRESN"
        assert FIELD_MAP["event_time"] == "DTHFOLL"
        assert FIELD_MAP["tir_percent"] == "BGSTRESN"


# ------------------------------------------------------------------ #
#  TASK 4 — PDF (text) report generator                               #
# ------------------------------------------------------------------ #

class TestPdfReport:
    """generate_prescreening_report returns structured text with required sections."""

    def _make_result(self, eligible=True):
        """Build a minimal EligibilityResult for testing."""
        from src.trials.protocol import load_protocol
        from src.trials.eligibility import screen_patient
        p = load_protocol("diamond_t1dm")
        metrics = {
            "patient_id":        "adult#001",
            "day":               "2026-10-01",
            "tir_percent":       55.0,
            "mean_glucose":      182.0,
            "hypo_events":       1,
            "severe_hypo_events": 0,
            "cv_percent":        28.0,
            "gmi":               7.66,
        }
        if not eligible:
            metrics["gmi"] = 5.5   # fails inclusion
            metrics["tir_percent"] = 90.0
        return screen_patient("adult#001", metrics, p)

    def test_report_returns_string(self):
        from src.reports.pdf_report import generate_prescreening_report
        result = self._make_result(eligible=True)
        report = generate_prescreening_report(
            result=result,
            screening_date=date(2026, 10, 8),
        )
        assert isinstance(report, str)
        assert len(report) > 100

    def test_report_contains_patient_id(self):
        from src.reports.pdf_report import generate_prescreening_report
        result = self._make_result()
        report = generate_prescreening_report(result=result, screening_date=date(2026, 10, 8))
        assert "adult#001" in report

    def test_report_contains_trial_name(self):
        from src.reports.pdf_report import generate_prescreening_report
        result = self._make_result()
        report = generate_prescreening_report(result=result, screening_date=date(2026, 10, 8))
        assert "DIAMOND" in report

    def test_report_contains_screening_date(self):
        from src.reports.pdf_report import generate_prescreening_report
        result = self._make_result()
        report = generate_prescreening_report(result=result, screening_date=date(2026, 10, 8))
        assert "2026" in report

    def test_report_contains_eligible_verdict_positive(self):
        from src.reports.pdf_report import generate_prescreening_report
        result = self._make_result(eligible=True)
        report = generate_prescreening_report(result=result, screening_date=date(2026, 10, 8))
        # Should mention candidacy/eligible status positively
        assert "CANDIDATE" in report.upper() or "ELIGIBLE" in report.upper()

    def test_report_contains_ineligible_verdict(self):
        from src.reports.pdf_report import generate_prescreening_report
        result = self._make_result(eligible=False)
        report = generate_prescreening_report(result=result, screening_date=date(2026, 10, 8))
        assert "NOT" in report.upper() or "INELIGIBLE" in report.upper()

    def test_report_lists_criteria(self):
        from src.reports.pdf_report import generate_prescreening_report
        result = self._make_result()
        report = generate_prescreening_report(result=result, screening_date=date(2026, 10, 8))
        # Must contain some criterion label text
        assert "GMI" in report or "HbA1c" in report or "gmi" in report

    def test_report_has_inclusion_section(self):
        from src.reports.pdf_report import generate_prescreening_report
        result = self._make_result()
        report = generate_prescreening_report(result=result, screening_date=date(2026, 10, 8))
        assert "INCLUSION" in report.upper() or "Inclusion" in report

    def test_report_has_exclusion_section(self):
        from src.reports.pdf_report import generate_prescreening_report
        result = self._make_result()
        report = generate_prescreening_report(result=result, screening_date=date(2026, 10, 8))
        assert "EXCLUSION" in report.upper() or "Exclusion" in report

    def test_save_report_writes_file(self, tmp_path):
        """save_report(result, path, screening_date) must write a file."""
        from src.reports.pdf_report import save_report
        result = self._make_result()
        out = tmp_path / "report.txt"
        save_report(result=result, output_path=out, screening_date=date(2026, 10, 8))
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert len(content) > 100

    def test_report_pass_fail_symbols(self):
        """Each criterion line should indicate PASS or FAIL clearly."""
        from src.reports.pdf_report import generate_prescreening_report
        result = self._make_result(eligible=True)
        report = generate_prescreening_report(result=result, screening_date=date(2026, 10, 8))
        assert "PASS" in report.upper() or "✓" in report or "[PASS]" in report
