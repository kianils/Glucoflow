"""
tests/test_repl.py — Test suite for Prompt 12: Conversational CLI REPL

Tests are split into four classes:
  TestIntentParser     — deterministic parsing of natural language queries
  TestIntentExtraction — patient ID and trial ID extraction from queries
  TestREPLHandleQuery  — end-to-end query handling through GlucoFlowREPL
  TestREPLFormatters   — output formatter functions (no I/O)

All tests are fully offline — no AWS, no S3, no Bedrock calls.
The MCPServer and BedrockClient are injected with mocks/stubs.
"""

import pytest
from unittest.mock import MagicMock, patch

from src.repl.intent_parser import IntentParser, ParsedIntent, Intent, TRIAL_ALIASES
from src.repl.repl import (
    GlucoFlowREPL,
    DEMO_COHORT,
    _fmt_screen_result,
    _fmt_find_result,
    _fmt_explanation,
    _fmt_comparison,
    _fmt_briefing,
)
from src.mcp.server import (
    ScreenPatientResponse,
    FindEligibleResponse,
    ExplainExclusionResponse,
    CompareTrialsResponse,
    CoordinatorBriefingResponse,
)


# ------------------------------------------------------------------ #
#  Shared helpers                                                      #
# ------------------------------------------------------------------ #

def _make_repl(cohort=None):
    """Return a GlucoFlowREPL in demo mode with optional custom cohort."""
    return GlucoFlowREPL(
        demo_mode=True,
        bedrock_client=None,
        cohort=cohort if cohort is not None else DEMO_COHORT,
    )


# ------------------------------------------------------------------ #
#  TestIntentParser                                                    #
# ------------------------------------------------------------------ #

class TestIntentParser:
    """Verify the intent parser resolves queries to the correct intent."""

    def setup_method(self):
        self.parser = IntentParser()

    # --- QUIT ---
    def test_quit_intent(self):
        assert self.parser.parse("quit").intent == Intent.QUIT

    def test_exit_intent(self):
        assert self.parser.parse("exit").intent == Intent.QUIT

    def test_q_intent(self):
        assert self.parser.parse("q").intent == Intent.QUIT

    # --- HELP ---
    def test_help_intent(self):
        assert self.parser.parse("help").intent == Intent.HELP

    def test_question_mark_intent(self):
        assert self.parser.parse("?").intent == Intent.HELP

    # --- LIST TRIALS ---
    def test_list_trials_intent(self):
        assert self.parser.parse("list trials").intent == Intent.LIST_TRIALS

    def test_available_trials_intent(self):
        assert self.parser.parse("what trials are available?").intent == Intent.LIST_TRIALS

    # --- LIST PATIENTS ---
    def test_list_patients_intent(self):
        assert self.parser.parse("list patients").intent == Intent.LIST_PATIENTS

    # --- COORDINATOR BRIEFING ---
    def test_briefing_keyword(self):
        assert self.parser.parse("give me a briefing").intent == Intent.COORDINATOR_BRIEFING

    def test_overview_keyword(self):
        assert self.parser.parse("recruitment overview").intent == Intent.COORDINATOR_BRIEFING

    def test_how_many_candidates(self):
        assert self.parser.parse("how many candidates do I have?").intent == Intent.COORDINATOR_BRIEFING

    # --- COMPARE TRIALS ---
    def test_compare_trials_with_patient(self):
        result = self.parser.parse("compare trials for adult_001")
        assert result.intent == Intent.COMPARE_TRIALS
        assert result.patient_id == "adult_001"

    def test_which_trial_suits_patient(self):
        result = self.parser.parse("which trial best suits child_001?")
        assert result.intent == Intent.COMPARE_TRIALS

    # --- EXPLAIN EXCLUSION ---
    def test_why_not_qualify(self):
        result = self.parser.parse("why doesn't child_001 qualify for the diamond trial?")
        assert result.intent == Intent.EXPLAIN_EXCLUSION

    def test_explain_exclusion_full(self):
        result = self.parser.parse("explain why adult_001 is not eligible for the glp1 trial")
        assert result.intent == Intent.EXPLAIN_EXCLUSION

    def test_explain_low_confidence_no_patient(self):
        result = self.parser.parse("why doesn't someone qualify?")
        assert result.intent == Intent.EXPLAIN_EXCLUSION
        assert result.confidence in ("medium", "low")

    # --- FIND ELIGIBLE ---
    def test_who_qualifies_for_trial(self):
        result = self.parser.parse("who qualifies for the diamond trial?")
        assert result.intent == Intent.FIND_ELIGIBLE
        assert result.trial_id == "diamond_t1dm"

    def test_find_candidates(self):
        result = self.parser.parse("find candidates for the closed-loop study")
        assert result.intent == Intent.FIND_ELIGIBLE

    def test_list_candidates_for_glp1(self):
        result = self.parser.parse("list candidates for glp1")
        assert result.intent == Intent.FIND_ELIGIBLE
        assert result.trial_id == "glp1_t2dm"

    def test_who_qualifies_no_trial_is_low_confidence(self):
        result = self.parser.parse("who qualifies?")
        assert result.confidence == "low"

    # --- SCREEN PATIENT ---
    def test_screen_patient_explicit(self):
        result = self.parser.parse("screen adult_001 for the diamond trial")
        assert result.intent == Intent.SCREEN_PATIENT
        assert result.patient_id == "adult_001"
        assert result.trial_id == "diamond_t1dm"

    def test_does_patient_qualify(self):
        result = self.parser.parse("does adolescent_001 qualify for the glp1 trial?")
        assert result.intent == Intent.SCREEN_PATIENT

    def test_check_patient_eligibility(self):
        result = self.parser.parse("check child_001 for the aps study")
        assert result.intent == Intent.SCREEN_PATIENT

    # --- UNKNOWN ---
    def test_numeric_shanghai_explain_query(self):
        result = self.parser.parse("why doesn't 1001 qualify for the diamond trial?")
        assert result.intent == Intent.EXPLAIN_EXCLUSION
        assert result.patient_id == "1001"
        assert result.trial_id == "diamond_t1dm"

    def test_numeric_shanghai_screen_query(self):
        result = self.parser.parse("screen 2002 for the glp1 trial")
        assert result.intent == Intent.SCREEN_PATIENT
        assert result.patient_id == "2002"
        assert result.trial_id == "glp1_t2dm"

    def test_numeric_shanghai_compare_query(self):
        result = self.parser.parse("compare all trials for 1006")
        assert result.intent == Intent.COMPARE_TRIALS
        assert result.patient_id == "1006"

    def test_year_is_not_patient_id(self):
        result = self.parser.parse("who qualifies for the diamond trial in 2021?")
        assert result.patient_id is None

    def test_unknown_gibberish(self):
        result = self.parser.parse("xyzzy frobnicator")
        assert result.intent == Intent.UNKNOWN

    def test_unknown_has_suggestion(self):
        result = self.parser.parse("xyzzy frobnicator")
        assert result.suggestion is not None


# ------------------------------------------------------------------ #
#  TestIntentExtraction                                                #
# ------------------------------------------------------------------ #

class TestIntentExtraction:
    """Verify patient and trial extraction from query strings."""

    def setup_method(self):
        self.parser = IntentParser()

    def test_extract_adolescent_001(self):
        r = self.parser.parse("screen adolescent_001 for the diamond trial")
        assert r.patient_id == "adolescent_001"

    def test_extract_adult_001(self):
        r = self.parser.parse("screen adult_001 for the glp1 trial")
        assert r.patient_id == "adult_001"

    def test_extract_child_001(self):
        r = self.parser.parse("why doesn't child_001 qualify?")
        assert r.patient_id == "child_001"

    def test_hash_notation_normalised(self):
        r = self.parser.parse("screen child#001 for the diamond trial")
        assert r.patient_id == "child_001"

    def test_extract_diamond_trial(self):
        r = self.parser.parse("who qualifies for the diamond trial?")
        assert r.trial_id == "diamond_t1dm"

    def test_extract_diamond_alias_t1dm(self):
        r = self.parser.parse("who qualifies for the t1dm trial?")
        assert r.trial_id == "diamond_t1dm"

    def test_extract_closed_loop(self):
        r = self.parser.parse("who qualifies for the closed loop study?")
        assert r.trial_id == "closed_loop_candidate"

    def test_extract_aps_alias(self):
        r = self.parser.parse("who qualifies for the aps trial?")
        assert r.trial_id == "closed_loop_candidate"

    def test_extract_glp1(self):
        r = self.parser.parse("find candidates for glp1")
        assert r.trial_id == "glp1_t2dm"

    def test_extract_sustain_alias(self):
        r = self.parser.parse("find candidates for the sustain trial")
        assert r.trial_id == "glp1_t2dm"

    def test_no_patient_returns_none(self):
        r = self.parser.parse("who qualifies for the diamond trial?")
        assert r.patient_id is None

    def test_no_trial_returns_none(self):
        r = self.parser.parse("screen adult_001")
        assert r.trial_id is None


# ------------------------------------------------------------------ #
#  TestREPLHandleQuery                                                 #
# ------------------------------------------------------------------ #

class TestREPLHandleQuery:
    """End-to-end query handling through GlucoFlowREPL.handle_query()."""

    def setup_method(self):
        self.repl = _make_repl()

    def test_help_returns_help_text(self):
        resp = self.repl.handle_query("help")
        assert "Available commands" in resp

    def test_list_trials_returns_trial_ids(self):
        resp = self.repl.handle_query("list trials")
        assert "diamond_t1dm" in resp
        assert "glp1_t2dm" in resp
        assert "closed_loop_candidate" in resp

    def test_list_patients_returns_patient_ids(self):
        resp = self.repl.handle_query("list patients")
        assert "adolescent_001" in resp
        assert "adult_001" in resp
        assert "child_001" in resp

    def test_screen_patient_returns_result(self):
        resp = self.repl.handle_query("screen adult_001 for the diamond trial")
        assert "adult_001" in resp
        assert "diamond_t1dm" in resp

    def test_screen_patient_shows_status(self):
        resp = self.repl.handle_query("screen adult_001 for the diamond trial")
        assert any(word in resp for word in ("CANDIDATE", "CRITERIA", "Status"))

    def test_find_eligible_returns_result(self):
        resp = self.repl.handle_query("who qualifies for the diamond trial?")
        assert "diamond" in resp.lower()

    def test_explain_exclusion_returns_explanation(self):
        resp = self.repl.handle_query(
            "why doesn't child_001 qualify for the diamond trial?"
        )
        assert "child_001" in resp

    def test_compare_trials_returns_comparison(self):
        resp = self.repl.handle_query("compare trials for adult_001")
        assert "adult_001" in resp
        assert any(t in resp for t in ("diamond", "glp1", "closed_loop"))

    def test_briefing_returns_cohort_info(self):
        resp = self.repl.handle_query("give me a briefing")
        assert any(word in resp for word in ("patient", "cohort", "trial", "Briefing"))

    def test_unknown_query_offers_suggestion(self):
        resp = self.repl.handle_query("xyzzy frobnicator blortz")
        assert any(word in resp for word in ("help", "sure", "understand", "?"))

    def test_missing_patient_gives_helpful_error(self):
        resp = self.repl.handle_query("screen patient_does_not_exist for the diamond trial")
        assert any(word in resp.lower() for word in ("not found", "missing", "need"))

    def test_audit_trail_disclaimer_in_screen(self):
        resp = self.repl.handle_query("screen adult_001 for the diamond trial")
        assert "pre-screening" in resp.lower() or "clinical" in resp.lower()

    def test_child_001_hypo_events_affect_eligibility(self):
        """child_001 has 2 severe hypo events — should fail diamond exclusion."""
        resp = self.repl.handle_query("screen child_001 for the diamond trial")
        # child_001 severe_hypo_events=2 > threshold 1 → excluded
        assert any(w in resp for w in ("✗", "NOT", "CRITERIA", "excluded", "not"))


# ------------------------------------------------------------------ #
#  TestREPLFormatters                                                  #
# ------------------------------------------------------------------ #

class TestREPLFormatters:
    """Unit tests for the pure formatter functions."""

    def _mock_screen_resp(self, eligible=True):
        return ScreenPatientResponse(
            patient_id="adult_001",
            trial_id="diamond_t1dm",
            eligible=eligible,
            summary="Patient adult_001 is a PRE-SCREENING CANDIDATE." if eligible else "Patient does NOT meet criteria.",
            narrative=None,
            audit_trail={},
        )

    def _mock_find_resp(self, count=2, total=3):
        return FindEligibleResponse(
            trial_id="diamond_t1dm",
            trial_name="DIAMOND CGM Trial",
            eligible_count=count,
            total_screened=total,
            candidates=[
                {"patient_id": "adult_001", "eligible": True,  "summary": "Meets criteria."},
                {"patient_id": "adolescent_001", "eligible": True, "summary": "Meets criteria."},
                {"patient_id": "child_001", "eligible": False, "summary": "Excluded."},
            ],
            population_summary="2 of 3 patients eligible.",
        )

    def _mock_explain_resp(self, eligible=False):
        return ExplainExclusionResponse(
            patient_id="child_001",
            trial_id="diamond_t1dm",
            eligible=eligible,
            plain_english_explanation="Patient has too many severe hypoglycaemic events.",
            failed_criteria=[
                {
                    "field": "severe_hypo_events",
                    "label": "More than 1 severe hypoglycaemic event",
                    "patient_value": 2.0,
                    "threshold": 1.0,
                    "required": "<= 1",
                    "clinical_rationale": "Safety concern.",
                }
            ],
            recommendation="Refer for clinical assessment before trial screening.",
        )

    def _mock_compare_resp(self):
        return CompareTrialsResponse(
            patient_id="adult_001",
            best_match="diamond_t1dm",
            comparison_table=[
                {"trial_id": "diamond_t1dm",         "eligible": True,  "criteria_passed": 3, "criteria_total": 5},
                {"trial_id": "glp1_t2dm",            "eligible": False, "criteria_passed": 1, "criteria_total": 6},
                {"trial_id": "closed_loop_candidate", "eligible": False, "criteria_passed": 0, "criteria_total": 5},
            ],
        )

    def _mock_briefing_resp(self):
        return CoordinatorBriefingResponse(
            total_patients=3,
            trials_assessed=["diamond_t1dm", "glp1_t2dm", "closed_loop_candidate"],
            briefing_text="2 of 3 patients are pre-screening candidates for at least one trial.",
            opportunities=[
                {"trial_id": "diamond_t1dm", "eligible_count": 2, "total_screened": 3, "top_candidates": [{"patient_id": "adult_001"}]},
                {"trial_id": "glp1_t2dm",    "eligible_count": 0, "total_screened": 3, "top_candidates": []},
            ],
        )

    def test_screen_result_eligible_contains_patient_id(self):
        assert "adult_001" in _fmt_screen_result(self._mock_screen_resp())

    def test_screen_result_eligible_shows_candidate(self):
        out = _fmt_screen_result(self._mock_screen_resp(eligible=True))
        assert "CANDIDATE" in out

    def test_screen_result_ineligible_shows_not(self):
        out = _fmt_screen_result(self._mock_screen_resp(eligible=False))
        assert "NOT" in out or "CRITERIA" in out

    def test_find_result_shows_eligible_count(self):
        out = _fmt_find_result(self._mock_find_resp())
        assert "2" in out

    def test_find_result_shows_trial_name(self):
        out = _fmt_find_result(self._mock_find_resp())
        assert "DIAMOND" in out

    def test_explanation_shows_patient(self):
        out = _fmt_explanation(self._mock_explain_resp())
        assert "child_001" in out

    def test_explanation_shows_failed_field(self):
        out = _fmt_explanation(self._mock_explain_resp())
        assert "severe" in out.lower() or "hypo" in out.lower()

    def test_explanation_shows_recommendation(self):
        out = _fmt_explanation(self._mock_explain_resp())
        assert "Recommendation" in out or "assessment" in out.lower()

    def test_comparison_shows_best_match(self):
        out = _fmt_comparison(self._mock_compare_resp())
        assert "diamond_t1dm" in out or "Best match" in out

    def test_comparison_shows_all_trials(self):
        out = _fmt_comparison(self._mock_compare_resp())
        assert "glp1_t2dm" in out
        assert "closed_loop_candidate" in out

    def test_briefing_shows_patient_count(self):
        out = _fmt_briefing(self._mock_briefing_resp())
        assert "3" in out

    def test_briefing_shows_trial_names(self):
        out = _fmt_briefing(self._mock_briefing_resp())
        assert "diamond_t1dm" in out
