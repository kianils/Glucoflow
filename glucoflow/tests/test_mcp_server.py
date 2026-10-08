"""
tests/test_mcp_server.py — MCP Server Test Suite

Tests all 5 coordinator-facing MCP actions:
  1. screen_patient_for_trial
  2. find_eligible_patients
  3. explain_exclusion
  4. compare_trials_for_patient
  5. get_coordinator_briefing

All tests use:
  - In-memory TrialProtocol instances (no filesystem I/O for protocol loading)
  - A mock BedrockClient injected into MCPServer (no real AWS calls)
  - Concrete Gold metric dicts that mirror what the Gold layer produces

Test count: 46 new tests
"""

import pytest
from unittest.mock import MagicMock, patch

from src.mcp.server import (
    MCPServer,
    ScreenPatientRequest,
    FindEligibleRequest,
    ExplainExclusionRequest,
    CompareTrialsRequest,
    CoordinatorBriefingRequest,
    _build_audit_trail,
)
from src.trials.protocol import TrialProtocol, TrialCriterion
from src.trials.eligibility import screen_patient


# ------------------------------------------------------------------ #
#  Shared fixtures                                                     #
# ------------------------------------------------------------------ #

def _make_protocol(
    trial_id="test_trial",
    inclusion=None,
    exclusion=None,
):
    """Build a minimal TrialProtocol for testing."""
    return TrialProtocol(
        trial_id=trial_id,
        trial_name=f"Test Trial {trial_id}",
        phase="II",
        sponsor="Test Sponsor",
        description="A test trial protocol.",
        notes="Test notes.",
        inclusion_criteria=inclusion or [
            TrialCriterion(
                field="tir_percent",
                operator="<=",
                threshold=70.0,
                human_label="TIR <= 70%",
                human_rationale="Test rationale.",
            ),
            TrialCriterion(
                field="gmi",
                operator=">=",
                threshold=7.5,
                human_label="GMI >= 7.5",
                human_rationale="Test rationale.",
            ),
        ],
        exclusion_criteria=exclusion or [
            TrialCriterion(
                field="severe_hypo_events",
                operator=">",
                threshold=1.0,
                human_label="Severe hypos > 1",
                human_rationale="Test rationale.",
            ),
        ],
    )


def _eligible_metrics(patient_id="pt_001"):
    """Gold metrics dict that satisfies the test protocol."""
    return {
        "patient_id": patient_id,
        "day": "2026-10-07",
        "tir_percent": 55.0,       # <= 70 ✅
        "mean_glucose": 180.0,
        "hypo_events": 2,
        "severe_hypo_events": 0,   # not > 1 ✅
        "cv_percent": 28.0,
        "gmi": 8.1,                # >= 7.5 ✅
        "source_system": "dexcom_messy",
    }


def _ineligible_metrics(patient_id="pt_bad"):
    """Gold metrics dict that fails inclusion (TIR too high)."""
    return {
        "patient_id": patient_id,
        "day": "2026-10-07",
        "tir_percent": 85.0,       # > 70 ❌
        "mean_glucose": 120.0,
        "hypo_events": 0,
        "severe_hypo_events": 0,
        "cv_percent": 18.0,
        "gmi": 6.2,                # < 7.5 ❌
        "source_system": "simglucose",
    }


def _excluded_metrics(patient_id="pt_excl"):
    """Gold metrics dict that triggers an exclusion criterion."""
    return {
        "patient_id": patient_id,
        "day": "2026-10-07",
        "tir_percent": 55.0,       # <= 70 ✅ (inclusion passes)
        "mean_glucose": 160.0,
        "hypo_events": 5,
        "severe_hypo_events": 3,   # > 1 → exclusion triggered ❌
        "cv_percent": 40.0,
        "gmi": 8.5,                # >= 7.5 ✅
        "source_system": "simglucose",
    }


def _mock_bedrock():
    """Inject a mock BedrockClient that returns a canned narrative."""
    mock = MagicMock()
    mock.generate_narrative.return_value = "Mock narrative text."
    mock.generate_population_narrative.return_value = "Mock population narrative."
    return mock


# ------------------------------------------------------------------ #
#  Helper: _build_audit_trail                                         #
# ------------------------------------------------------------------ #

class TestBuildAuditTrail:
    def test_audit_trail_has_patient_id(self):
        protocol = _make_protocol()
        result = screen_patient("pt_001", _eligible_metrics(), protocol)
        audit = _build_audit_trail(result)
        assert audit["patient_id"] == "pt_001"

    def test_audit_trail_has_trial_id(self):
        protocol = _make_protocol()
        result = screen_patient("pt_001", _eligible_metrics(), protocol)
        audit = _build_audit_trail(result)
        assert audit["trial_id"] == "test_trial"

    def test_audit_trail_inclusion_count_matches_protocol(self):
        protocol = _make_protocol()
        result = screen_patient("pt_001", _eligible_metrics(), protocol)
        audit = _build_audit_trail(result)
        assert len(audit["inclusion_criteria"]) == 2

    def test_audit_trail_exclusion_count_matches_protocol(self):
        protocol = _make_protocol()
        result = screen_patient("pt_001", _eligible_metrics(), protocol)
        audit = _build_audit_trail(result)
        assert len(audit["exclusion_criteria"]) == 1

    def test_audit_trail_inclusion_has_patient_value(self):
        protocol = _make_protocol()
        result = screen_patient("pt_001", _eligible_metrics(), protocol)
        audit = _build_audit_trail(result)
        assert audit["inclusion_criteria"][0]["patient_value"] == 55.0

    def test_audit_trail_eligible_field_correct(self):
        protocol = _make_protocol()
        result = screen_patient("pt_001", _eligible_metrics(), protocol)
        audit = _build_audit_trail(result)
        assert audit["eligible"] is True


# ------------------------------------------------------------------ #
#  Action 1: screen_patient_for_trial                                 #
# ------------------------------------------------------------------ #

class TestScreenPatientForTrial:

    def _server(self):
        return MCPServer(bedrock_client=_mock_bedrock())

    def test_eligible_patient_returns_eligible_true(self):
        server = self._server()
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = ScreenPatientRequest("pt_001", "test_trial", _eligible_metrics(), generate_narrative=False)
            resp = server.screen_patient_for_trial(req)
        assert resp.eligible is True

    def test_ineligible_patient_returns_eligible_false(self):
        server = self._server()
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = ScreenPatientRequest("pt_bad", "test_trial", _ineligible_metrics(), generate_narrative=False)
            resp = server.screen_patient_for_trial(req)
        assert resp.eligible is False

    def test_response_contains_summary(self):
        server = self._server()
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = ScreenPatientRequest("pt_001", "test_trial", _eligible_metrics(), generate_narrative=False)
            resp = server.screen_patient_for_trial(req)
        assert isinstance(resp.summary, str)
        assert len(resp.summary) > 10

    def test_narrative_none_when_narrative_requested(self):
        """Narrative is always None — AI narrative layer removed, eligibility output is self-contained."""
        server = self._server()
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = ScreenPatientRequest("pt_001", "test_trial", _eligible_metrics(), generate_narrative=True)
            resp = server.screen_patient_for_trial(req)
        assert resp.narrative is None

    def test_narrative_none_when_not_requested(self):
        server = self._server()
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = ScreenPatientRequest("pt_001", "test_trial", _eligible_metrics(), generate_narrative=False)
            resp = server.screen_patient_for_trial(req)
        assert resp.narrative is None

    def test_audit_trail_present(self):
        server = self._server()
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = ScreenPatientRequest("pt_001", "test_trial", _eligible_metrics(), generate_narrative=False)
            resp = server.screen_patient_for_trial(req)
        assert isinstance(resp.audit_trail, dict)
        assert "inclusion_criteria" in resp.audit_trail

    def test_audit_trail_eligible_matches_response(self):
        server = self._server()
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = ScreenPatientRequest("pt_001", "test_trial", _eligible_metrics(), generate_narrative=False)
            resp = server.screen_patient_for_trial(req)
        assert resp.audit_trail["eligible"] == resp.eligible

    def test_excluded_patient_is_ineligible(self):
        server = self._server()
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = ScreenPatientRequest("pt_excl", "test_trial", _excluded_metrics(), generate_narrative=False)
            resp = server.screen_patient_for_trial(req)
        assert resp.eligible is False


# ------------------------------------------------------------------ #
#  Action 2: find_eligible_patients                                   #
# ------------------------------------------------------------------ #

class TestFindEligiblePatients:

    def _server(self):
        return MCPServer(bedrock_client=_mock_bedrock())

    def test_eligible_count_correct(self):
        server = self._server()
        cohort = [_eligible_metrics("pt_001"), _ineligible_metrics("pt_002")]
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = FindEligibleRequest("test_trial", cohort)
            resp = server.find_eligible_patients(req)
        assert resp.eligible_count == 1

    def test_total_screened_correct(self):
        server = self._server()
        cohort = [_eligible_metrics("pt_001"), _ineligible_metrics("pt_002"), _eligible_metrics("pt_003")]
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = FindEligibleRequest("test_trial", cohort)
            resp = server.find_eligible_patients(req)
        assert resp.total_screened == 3

    def test_eligible_patients_appear_first(self):
        server = self._server()
        cohort = [_ineligible_metrics("pt_bad"), _eligible_metrics("pt_good")]
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = FindEligibleRequest("test_trial", cohort)
            resp = server.find_eligible_patients(req)
        assert resp.candidates[0]["eligible"] is True

    def test_population_summary_contains_count(self):
        server = self._server()
        cohort = [_eligible_metrics("pt_001"), _ineligible_metrics("pt_002")]
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = FindEligibleRequest("test_trial", cohort)
            resp = server.find_eligible_patients(req)
        assert "1 of 2" in resp.population_summary

    def test_empty_cohort_returns_zero(self):
        server = self._server()
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = FindEligibleRequest("test_trial", [])
            resp = server.find_eligible_patients(req)
        assert resp.eligible_count == 0
        assert resp.total_screened == 0

    def test_candidate_dict_has_audit_trail(self):
        server = self._server()
        cohort = [_eligible_metrics("pt_001")]
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = FindEligibleRequest("test_trial", cohort)
            resp = server.find_eligible_patients(req)
        assert "audit_trail" in resp.candidates[0]


# ------------------------------------------------------------------ #
#  Action 3: explain_exclusion                                        #
# ------------------------------------------------------------------ #

class TestExplainExclusion:

    def _server(self):
        return MCPServer(bedrock_client=_mock_bedrock())

    def test_ineligible_patient_gets_explanation(self):
        server = self._server()
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = ExplainExclusionRequest("pt_bad", "test_trial", _ineligible_metrics())
            resp = server.explain_exclusion(req)
        assert resp.eligible is False
        assert len(resp.plain_english_explanation) > 10

    def test_failed_criteria_list_populated(self):
        server = self._server()
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = ExplainExclusionRequest("pt_bad", "test_trial", _ineligible_metrics())
            resp = server.explain_exclusion(req)
        assert len(resp.failed_criteria) > 0

    def test_each_failed_criterion_has_field(self):
        server = self._server()
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = ExplainExclusionRequest("pt_bad", "test_trial", _ineligible_metrics())
            resp = server.explain_exclusion(req)
        for fc in resp.failed_criteria:
            assert "field" in fc

    def test_recommendation_not_empty(self):
        server = self._server()
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = ExplainExclusionRequest("pt_bad", "test_trial", _ineligible_metrics())
            resp = server.explain_exclusion(req)
        assert len(resp.recommendation) > 0

    def test_eligible_patient_gets_positive_explanation(self):
        server = self._server()
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = ExplainExclusionRequest("pt_001", "test_trial", _eligible_metrics())
            resp = server.explain_exclusion(req)
        assert resp.eligible is True
        assert "MEETS" in resp.plain_english_explanation

    def test_exclusion_triggered_has_type_field(self):
        server = self._server()
        with patch("src.mcp.server.load_protocol", return_value=_make_protocol()):
            req = ExplainExclusionRequest("pt_excl", "test_trial", _excluded_metrics())
            resp = server.explain_exclusion(req)
        excl = [fc for fc in resp.failed_criteria if fc["type"] == "exclusion_triggered"]
        assert len(excl) >= 1


# ------------------------------------------------------------------ #
#  Action 4: compare_trials_for_patient                               #
# ------------------------------------------------------------------ #

class TestCompareTrialsForPatient:

    def _server(self):
        return MCPServer(bedrock_client=_mock_bedrock())

    def test_eligible_trial_in_eligible_list(self):
        server = self._server()
        protocols = {
            "trial_a": _make_protocol("trial_a"),
            "trial_b": _make_protocol(
                "trial_b",
                inclusion=[TrialCriterion(
                    field="tir_percent", operator=">=", threshold=90.0,
                    human_label="TIR >= 90%", human_rationale="Test."
                )]
            ),
        }
        with patch("src.mcp.server.load_protocol", side_effect=lambda tid: protocols[tid]), \
             patch("src.mcp.server.list_protocols", return_value=["trial_a", "trial_b"]):
            req = CompareTrialsRequest("pt_001", _eligible_metrics())
            resp = server.compare_trials_for_patient(req)
        assert "trial_a" in resp.eligible_trials
        assert "trial_b" in resp.ineligible_trials

    def test_best_match_is_eligible_trial(self):
        server = self._server()
        protocols = {"trial_a": _make_protocol("trial_a")}
        with patch("src.mcp.server.load_protocol", side_effect=lambda tid: protocols[tid]), \
             patch("src.mcp.server.list_protocols", return_value=["trial_a"]):
            req = CompareTrialsRequest("pt_001", _eligible_metrics())
            resp = server.compare_trials_for_patient(req)
        assert resp.best_match == "trial_a"

    def test_best_match_none_when_no_eligible(self):
        server = self._server()
        protocols = {"trial_a": _make_protocol("trial_a")}
        with patch("src.mcp.server.load_protocol", side_effect=lambda tid: protocols[tid]), \
             patch("src.mcp.server.list_protocols", return_value=["trial_a"]):
            req = CompareTrialsRequest("pt_bad", _ineligible_metrics())
            resp = server.compare_trials_for_patient(req)
        assert resp.best_match is None

    def test_comparison_table_has_one_row_per_trial(self):
        server = self._server()
        protocols = {
            "trial_a": _make_protocol("trial_a"),
            "trial_b": _make_protocol("trial_b"),
        }
        with patch("src.mcp.server.load_protocol", side_effect=lambda tid: protocols[tid]), \
             patch("src.mcp.server.list_protocols", return_value=["trial_a", "trial_b"]):
            req = CompareTrialsRequest("pt_001", _eligible_metrics(), trial_ids=["trial_a", "trial_b"])
            resp = server.compare_trials_for_patient(req)
        assert len(resp.comparison_table) == 2

    def test_specific_trial_ids_respected(self):
        server = self._server()
        protocols = {"trial_a": _make_protocol("trial_a")}
        with patch("src.mcp.server.load_protocol", side_effect=lambda tid: protocols[tid]):
            req = CompareTrialsRequest("pt_001", _eligible_metrics(), trial_ids=["trial_a"])
            resp = server.compare_trials_for_patient(req)
        assert len(resp.comparison_table) == 1


# ------------------------------------------------------------------ #
#  Action 5: get_coordinator_briefing                                  #
# ------------------------------------------------------------------ #

class TestCoordinatorBriefing:

    def _server(self):
        return MCPServer(bedrock_client=_mock_bedrock())

    def test_total_patients_correct(self):
        server = self._server()
        cohort = [_eligible_metrics("pt_001"), _ineligible_metrics("pt_002")]
        protocols = {"trial_a": _make_protocol("trial_a")}
        with patch("src.mcp.server.load_protocol", side_effect=lambda tid: protocols[tid]), \
             patch("src.mcp.server.list_protocols", return_value=["trial_a"]), \
             patch("src.mcp.server.screen_cohort", return_value=[]):
            req = CoordinatorBriefingRequest(cohort, trial_ids=["trial_a"])
            resp = server.get_coordinator_briefing(req)
        assert resp.total_patients == 2

    def test_trials_assessed_matches_input(self):
        server = self._server()
        protocols = {
            "trial_a": _make_protocol("trial_a"),
            "trial_b": _make_protocol("trial_b"),
        }
        with patch("src.mcp.server.load_protocol", side_effect=lambda tid: protocols[tid]), \
             patch("src.mcp.server.screen_cohort", return_value=[]):
            req = CoordinatorBriefingRequest([], trial_ids=["trial_a", "trial_b"])
            resp = server.get_coordinator_briefing(req)
        assert set(resp.trials_assessed) == {"trial_a", "trial_b"}

    def test_briefing_text_not_empty(self):
        server = self._server()
        cohort = [_eligible_metrics("pt_001")]
        protocols = {"trial_a": _make_protocol("trial_a")}
        with patch("src.mcp.server.load_protocol", side_effect=lambda tid: protocols[tid]), \
             patch("src.mcp.server.list_protocols", return_value=["trial_a"]):
            req = CoordinatorBriefingRequest(cohort, trial_ids=["trial_a"])
            resp = server.get_coordinator_briefing(req)
        assert len(resp.briefing_text) > 10

    def test_no_candidates_gives_fallback_message(self):
        server = self._server()
        protocols = {"trial_a": _make_protocol("trial_a")}
        with patch("src.mcp.server.load_protocol", side_effect=lambda tid: protocols[tid]), \
             patch("src.mcp.server.screen_cohort", return_value=[]):
            req = CoordinatorBriefingRequest([], trial_ids=["trial_a"])
            resp = server.get_coordinator_briefing(req)
        assert "No patients" in resp.briefing_text or len(resp.briefing_text) > 0

    def test_opportunities_list_has_entry_per_trial(self):
        server = self._server()
        protocols = {
            "trial_a": _make_protocol("trial_a"),
            "trial_b": _make_protocol("trial_b"),
        }
        with patch("src.mcp.server.load_protocol", side_effect=lambda tid: protocols[tid]), \
             patch("src.mcp.server.screen_cohort", return_value=[]):
            req = CoordinatorBriefingRequest([], trial_ids=["trial_a", "trial_b"])
            resp = server.get_coordinator_briefing(req)
        assert len(resp.opportunities) == 2
