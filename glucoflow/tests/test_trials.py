"""
test_trials.py — Test suite for src/trials/protocol.py and src/trials/eligibility.py

Coverage:
  TestOperatorEvaluation   : _apply_operator for all 6 operators
  TestCriterionEvaluation  : evaluate_criterion — pass, fail, missing metric
  TestScreenPatient        : screen_patient — eligible, inclusion fail, exclusion triggered, combined
  TestScreenCohort         : screen_cohort — ordering, mixed eligibility
  TestProtocolLoader       : load_protocol — valid ID, missing file, list_protocols
  TestRealProtocols        : load all 3 JSON protocols, verify schema validates
  TestDiamondProtocol      : DIAMOND trial — known eligible and ineligible patient scenarios
  TestClosedLoopProtocol   : Closed-loop — known eligible and ineligible patient scenarios
  TestGlp1Protocol         : GLP-1 T2DM — known eligible and ineligible patient scenarios

All tests are pure — zero AWS calls, zero file I/O except protocol JSON loading.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.trials.protocol import (
    TrialCriterion,
    TrialProtocol,
    load_protocol,
    list_protocols,
)
from src.trials.eligibility import (
    _apply_operator,
    CriterionResult,
    EligibilityResult,
    evaluate_criterion,
    screen_patient,
    screen_cohort,
)


# ------------------------------------------------------------------ #
#  Fixtures                                                            #
# ------------------------------------------------------------------ #

def _make_criterion(
    field="tir_percent",
    operator="<=",
    threshold=70.0,
    label="TIR ≤ 70%",
    rationale="Test rationale.",
) -> TrialCriterion:
    return TrialCriterion(
        field=field,
        operator=operator,
        threshold=threshold,
        human_label=label,
        human_rationale=rationale,
    )


def _make_protocol(
    inclusion=None,
    exclusion=None,
    trial_id="TEST_TRIAL",
) -> TrialProtocol:
    return TrialProtocol(
        trial_id=trial_id,
        trial_name="Test Trial",
        phase="II",
        sponsor="Test Sponsor",
        description="A test trial protocol.",
        inclusion_criteria=inclusion or [],
        exclusion_criteria=exclusion or [],
    )


def _eligible_diamond_patient() -> dict:
    """Gold metrics that satisfy all DIAMOND inclusion and no exclusion criteria."""
    return {
        "patient_id": "adult#001",
        "day": "2026-10-07",
        "tir_percent": 55.0,       # ≤ 70% ✅ inclusion
        "mean_glucose": 182.0,
        "hypo_events": 1,
        "severe_hypo_events": 0,   # ≤ 1 ✅ no exclusion
        "cv_percent": 28.0,
        "gmi": 7.66,               # 7.5–10.0 ✅ inclusion
        "source_system": "simglucose",
        "diabetes_type": "T1DM",   # ✅ v2: T1DM required for DIAMOND
    }


def _ineligible_diamond_patient() -> dict:
    """Gold metrics that fail DIAMOND inclusion (TIR too high)."""
    return {
        "patient_id": "healthy_patient",
        "day": "2026-10-07",
        "tir_percent": 85.0,       # > 70% ❌ fails inclusion
        "mean_glucose": 110.0,
        "hypo_events": 0,
        "severe_hypo_events": 0,
        "cv_percent": 18.0,
        "gmi": 5.9,                # < 7.5 ❌ also fails inclusion
        "source_system": "simglucose",
    }


def _excluded_diamond_patient() -> dict:
    """Gold metrics that pass inclusion but trigger DIAMOND exclusion (severe hypos)."""
    return {
        "patient_id": "brittle_patient",
        "day": "2026-10-07",
        "tir_percent": 48.0,       # ≤ 70% ✅ inclusion
        "mean_glucose": 195.0,
        "hypo_events": 6,
        "severe_hypo_events": 3,   # > 1 ❌ triggers exclusion
        "cv_percent": 31.0,
        "gmi": 7.98,               # 7.5–10.0 ✅ inclusion
        "source_system": "simglucose",
    }


# ------------------------------------------------------------------ #
#  TestOperatorEvaluation                                              #
# ------------------------------------------------------------------ #

class TestOperatorEvaluation:
    def test_gte_passes(self):
        assert _apply_operator(7.5, ">=", 7.5) is True

    def test_gte_fails(self):
        assert _apply_operator(7.4, ">=", 7.5) is False

    def test_lte_passes(self):
        assert _apply_operator(70.0, "<=", 70.0) is True

    def test_lte_fails(self):
        assert _apply_operator(71.0, "<=", 70.0) is False

    def test_gt_passes(self):
        assert _apply_operator(2.0, ">", 1.0) is True

    def test_lt_passes(self):
        assert _apply_operator(0.5, "<", 1.0) is True

    def test_eq_passes(self):
        assert _apply_operator(5.0, "==", 5.0) is True

    def test_ne_passes(self):
        assert _apply_operator(5.0, "!=", 4.0) is True

    def test_invalid_operator_raises(self):
        with pytest.raises(ValueError, match="Unsupported operator"):
            _apply_operator(5.0, "~=", 5.0)


# ------------------------------------------------------------------ #
#  TestCriterionEvaluation                                             #
# ------------------------------------------------------------------ #

class TestCriterionEvaluation:
    def test_passes_when_satisfied(self):
        c = _make_criterion(field="tir_percent", operator="<=", threshold=70.0)
        result = evaluate_criterion(c, {"tir_percent": 55.0})
        assert result.passed is True
        assert result.patient_value == 55.0
        assert result.failure_reason is None

    def test_fails_when_not_satisfied(self):
        c = _make_criterion(field="tir_percent", operator="<=", threshold=70.0)
        result = evaluate_criterion(c, {"tir_percent": 85.0})
        assert result.passed is False
        assert result.failure_reason is not None
        assert "85.00" in result.failure_reason

    def test_missing_metric_fails(self):
        c = _make_criterion(field="gmi")
        result = evaluate_criterion(c, {})  # no gmi key
        assert result.passed is False
        assert result.patient_value is None
        assert "unavailable" in result.failure_reason

    def test_none_metric_fails(self):
        c = _make_criterion(field="gmi")
        result = evaluate_criterion(c, {"gmi": None})
        assert result.passed is False
        assert result.patient_value is None

    def test_exclusion_failure_reason_wording(self):
        c = _make_criterion(
            field="severe_hypo_events",
            operator=">",
            threshold=1.0,
            label="Severe hypos > 1",
        )
        result = evaluate_criterion(c, {"severe_hypo_events": 0}, criterion_type="exclusion")
        # 0 > 1 is False — exclusion not triggered, passed=False in exclusion context
        assert result.passed is False


# ------------------------------------------------------------------ #
#  TestScreenPatient                                                   #
# ------------------------------------------------------------------ #

class TestScreenPatient:
    def test_fully_eligible(self):
        protocol = _make_protocol(
            inclusion=[_make_criterion("tir_percent", "<=", 70.0)],
            exclusion=[_make_criterion("severe_hypo_events", ">", 1.0)],
        )
        metrics = {"patient_id": "p1", "tir_percent": 55.0, "severe_hypo_events": 0}
        result = screen_patient("p1", metrics, protocol)
        assert result.eligible is True
        assert "PRE-SCREENING CANDIDATE" in result.summary

    def test_fails_inclusion(self):
        protocol = _make_protocol(
            inclusion=[_make_criterion("gmi", ">=", 7.5)],
        )
        metrics = {"patient_id": "p2", "gmi": 6.0}
        result = screen_patient("p2", metrics, protocol)
        assert result.eligible is False
        assert len(result.failed_inclusion) == 1
        assert "does NOT meet" in result.summary

    def test_exclusion_triggered(self):
        protocol = _make_protocol(
            inclusion=[_make_criterion("tir_percent", "<=", 70.0)],
            exclusion=[_make_criterion("severe_hypo_events", ">", 1.0)],
        )
        metrics = {"patient_id": "p3", "tir_percent": 50.0, "severe_hypo_events": 3}
        result = screen_patient("p3", metrics, protocol)
        assert result.eligible is False
        assert len(result.exclusion_triggered) == 1

    def test_no_criteria_eligible(self):
        """A protocol with no criteria — everyone is eligible."""
        protocol = _make_protocol()
        result = screen_patient("p4", {"patient_id": "p4"}, protocol)
        assert result.eligible is True

    def test_patient_id_in_result(self):
        protocol = _make_protocol()
        result = screen_patient("test_patient_99", {}, protocol)
        assert result.patient_id == "test_patient_99"

    def test_trial_id_in_result(self):
        protocol = _make_protocol(trial_id="MY_TRIAL")
        result = screen_patient("p5", {}, protocol)
        assert result.trial_id == "MY_TRIAL"

    def test_passed_inclusion_property(self):
        protocol = _make_protocol(
            inclusion=[
                _make_criterion("tir_percent", "<=", 70.0),
                _make_criterion("gmi", ">=", 7.5),
            ]
        )
        metrics = {"patient_id": "p6", "tir_percent": 55.0, "gmi": 8.0}
        result = screen_patient("p6", metrics, protocol)
        assert len(result.passed_inclusion) == 2
        assert len(result.failed_inclusion) == 0


# ------------------------------------------------------------------ #
#  TestScreenCohort                                                    #
# ------------------------------------------------------------------ #

class TestScreenCohort:
    def test_eligible_patients_first(self):
        protocol = _make_protocol(
            inclusion=[_make_criterion("tir_percent", "<=", 70.0)]
        )
        cohort = [
            {"patient_id": "z_ineligible", "tir_percent": 90.0},
            {"patient_id": "a_eligible",   "tir_percent": 50.0},
        ]
        results = screen_cohort(cohort, protocol)
        assert results[0].patient_id == "a_eligible"
        assert results[1].patient_id == "z_ineligible"

    def test_returns_one_result_per_patient(self):
        protocol = _make_protocol()
        cohort = [{"patient_id": f"p{i}"} for i in range(5)]
        results = screen_cohort(cohort, protocol)
        assert len(results) == 5

    def test_empty_cohort(self):
        protocol = _make_protocol()
        results = screen_cohort([], protocol)
        assert results == []


# ------------------------------------------------------------------ #
#  TestProtocolLoader                                                  #
# ------------------------------------------------------------------ #

class TestProtocolLoader:
    def test_load_valid_protocol(self):
        protocol = load_protocol("diamond_t1dm")
        assert protocol.trial_id == "diamond_t1dm"
        assert len(protocol.inclusion_criteria) > 0
        assert len(protocol.exclusion_criteria) > 0

    def test_load_missing_protocol_raises(self):
        with pytest.raises(FileNotFoundError, match="not found"):
            load_protocol("nonexistent_trial_xyz")

    def test_list_protocols_returns_all_three(self):
        protocols = list_protocols()
        assert "diamond_t1dm" in protocols
        assert "closed_loop_candidate" in protocols
        assert "glp1_t2dm" in protocols

    def test_all_protocols_load_without_error(self):
        for pid in list_protocols():
            p = load_protocol(pid)
            assert isinstance(p, TrialProtocol)
            assert p.trial_id == pid


# ------------------------------------------------------------------ #
#  TestRealProtocols — validate JSON schema correctness               #
# ------------------------------------------------------------------ #

class TestRealProtocols:
    def test_diamond_has_correct_fields(self):
        p = load_protocol("diamond_t1dm")
        assert p.phase == "III"
        # Check inclusion includes TIR and GMI fields
        inc_fields = {c.field for c in p.inclusion_criteria}
        assert "tir_percent" in inc_fields
        assert "gmi" in inc_fields

    def test_closed_loop_has_cv_inclusion(self):
        p = load_protocol("closed_loop_candidate")
        inc_fields = {c.field for c in p.inclusion_criteria}
        assert "cv_percent" in inc_fields

    def test_glp1_has_mean_glucose_inclusion(self):
        p = load_protocol("glp1_t2dm")
        inc_fields = {c.field for c in p.inclusion_criteria}
        assert "mean_glucose" in inc_fields

    def test_all_criteria_have_rationale(self):
        """Every criterion in every protocol must have a non-empty rationale."""
        for pid in list_protocols():
            p = load_protocol(pid)
            for c in p.inclusion_criteria + p.exclusion_criteria:
                assert len(c.human_rationale) > 10, (
                    f"Protocol {pid}: criterion '{c.human_label}' has empty rationale"
                )


# ------------------------------------------------------------------ #
#  TestDiamondProtocol — end-to-end known scenarios                   #
# ------------------------------------------------------------------ #

class TestDiamondProtocol:
    def test_eligible_patient(self):
        p = load_protocol("diamond_t1dm")
        result = screen_patient("adult#001", _eligible_diamond_patient(), p)
        assert result.eligible is True

    def test_ineligible_patient_low_gmi(self):
        p = load_protocol("diamond_t1dm")
        result = screen_patient("healthy", _ineligible_diamond_patient(), p)
        assert result.eligible is False
        failed_fields = {r.criterion.field for r in result.failed_inclusion}
        assert "gmi" in failed_fields

    def test_excluded_patient_severe_hypos(self):
        p = load_protocol("diamond_t1dm")
        result = screen_patient("brittle", _excluded_diamond_patient(), p)
        assert result.eligible is False
        excl_fields = {r.criterion.field for r in result.exclusion_triggered}
        assert "severe_hypo_events" in excl_fields


# ------------------------------------------------------------------ #
#  TestClosedLoopProtocol                                              #
# ------------------------------------------------------------------ #

class TestClosedLoopProtocol:
    def test_high_variability_patient_eligible(self):
        p = load_protocol("closed_loop_candidate")
        metrics = {
            "patient_id": "child#001",
            "tir_percent": 55.0,
            "mean_glucose": 160.0,
            "hypo_events": 5,
            "severe_hypo_events": 1,
            "cv_percent": 42.0,    # ≥ 36% ✅
            "gmi": 7.1,
            "diabetes_type": "T1DM",  # ✅ v2: T1DM required for closed-loop
        }
        result = screen_patient("child#001", metrics, p)
        assert result.eligible is True

    def test_stable_patient_not_eligible(self):
        p = load_protocol("closed_loop_candidate")
        metrics = {
            "patient_id": "stable_adult",
            "tir_percent": 80.0,   # > 65% ❌
            "mean_glucose": 120.0,
            "hypo_events": 0,      # < 3 ❌
            "severe_hypo_events": 0,
            "cv_percent": 18.0,    # < 36% ❌
            "gmi": 5.8,
        }
        result = screen_patient("stable_adult", metrics, p)
        assert result.eligible is False


# ------------------------------------------------------------------ #
#  TestGlp1Protocol                                                    #
# ------------------------------------------------------------------ #

class TestGlp1Protocol:
    def test_typical_t2dm_patient_eligible(self):
        p = load_protocol("glp1_t2dm")
        metrics = {
            "patient_id": "t2dm_adult",
            "tir_percent": 52.0,   # ≤ 70% ✅
            "mean_glucose": 190.0, # ≥ 154 ✅
            "hypo_events": 0,
            "severe_hypo_events": 0,  # no excl ✅
            "cv_percent": 22.0,
            "gmi": 7.85,           # 7.5–10.5 ✅
            "diabetes_type": "T2DM",  # ✅ v2: T2DM required for GLP-1 trial
        }
        result = screen_patient("t2dm_adult", metrics, p)
        assert result.eligible is True

    def test_well_controlled_t2dm_not_eligible(self):
        p = load_protocol("glp1_t2dm")
        metrics = {
            "patient_id": "controlled_t2dm",
            "tir_percent": 82.0,   # > 70% ❌
            "mean_glucose": 115.0, # < 154 ❌
            "hypo_events": 0,
            "severe_hypo_events": 0,
            "cv_percent": 14.0,
            "gmi": 6.1,            # < 7.5 ❌
        }
        result = screen_patient("controlled_t2dm", metrics, p)
        assert result.eligible is False
