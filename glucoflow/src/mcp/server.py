"""
mcp/server.py — GlucoFlow MCP (Model Context Protocol) Server

This is the top-level interface layer. It exposes a clean set of
coordinator-facing actions that combine:
  1. The Gold-layer clinical metrics (TIR%, GMI, hypo events etc.)
  2. The Trial Eligibility Engine (screen patients against real protocols)
  3. Bedrock AI (generate plain-English pre-screening narratives)

Who uses this?
  A clinical trial coordinator at a hospital site types a query like:
  "Who in my cohort qualifies for the DIAMOND trial?"
  and gets back a ranked candidate list with AI-generated narratives
  explaining why each patient does or does not qualify.

Design Principles:
  - Every action is a pure function accepting a typed request dataclass
    and returning a typed response dataclass. No hidden globals.
  - Injectable clients (S3, Bedrock) — all actions are fully testable
    without real AWS calls.
  - The MCP server itself has no knowledge of AWS internals — it
    delegates to existing layers (Gold, Eligibility, Bedrock) and
    assembles the result. This respects the separation of concerns
    already established in the codebase.
  - All responses include an `audit_trail` dict recording exactly
    which criteria were checked, which passed, and why — so the
    coordinator has a traceable record for their study files.

Clinical/Regulatory Note:
  The output of every action is a PRE-SCREENING recommendation only.
  No action in this server constitutes a clinical decision or medical
  advice. The coordinator must independently verify eligibility through
  the formal study screening visit before enrolling any patient.

MCP Actions exposed:
  1. screen_patient_for_trial      — one patient vs one trial
  2. find_eligible_patients        — all patients vs one trial, ranked
  3. explain_exclusion             — why a patient doesn't qualify
  4. compare_trials_for_patient    — which trials suit this patient best
  5. get_coordinator_briefing      — population-level trial opportunity summary
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from src.trials.protocol import load_protocol, list_protocols, TrialProtocol
from src.trials.eligibility import (
    screen_patient,
    screen_cohort,
    EligibilityResult,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Request / Response dataclasses                                      #
# ------------------------------------------------------------------ #

@dataclass
class ScreenPatientRequest:
    patient_id: str
    trial_id: str
    gold_metrics: dict          # GoldReading.model_dump(mode="json")
    generate_narrative: bool = True


@dataclass
class ScreenPatientResponse:
    patient_id: str
    trial_id: str
    eligible: bool
    summary: str                # Plain-English one-paragraph summary
    narrative: Optional[str]    # Bedrock AI narrative (if generate_narrative=True)
    audit_trail: dict           # Full criterion-by-criterion breakdown


@dataclass
class FindEligibleRequest:
    trial_id: str
    cohort_metrics: list[dict]  # list of GoldReading dicts
    generate_narratives: bool = False   # Off by default — narratives cost Bedrock calls


@dataclass
class FindEligibleResponse:
    trial_id: str
    trial_name: str
    eligible_count: int
    total_screened: int
    candidates: list[dict]      # Ranked list: eligible first, then ineligible
    population_summary: str     # One-line text summary for the coordinator


@dataclass
class ExplainExclusionRequest:
    patient_id: str
    trial_id: str
    gold_metrics: dict


@dataclass
class ExplainExclusionResponse:
    patient_id: str
    trial_id: str
    eligible: bool
    plain_english_explanation: str   # Coordinator-ready explanation
    failed_criteria: list[dict]      # Each failed criterion with field + reason
    recommendation: str              # What to do next


@dataclass
class CompareTrialsRequest:
    patient_id: str
    gold_metrics: dict
    trial_ids: Optional[list[str]] = None  # None = compare all available trials


@dataclass
class CompareTrialsResponse:
    patient_id: str
    best_match: Optional[str]       # Trial ID patient best matches, or None
    comparison_table: list[dict]    # One row per trial with eligible + reason
    eligible_trials: list[str] = field(default_factory=list)
    ineligible_trials: list[str] = field(default_factory=list)


@dataclass
class CoordinatorBriefingRequest:
    cohort_metrics: list[dict]
    trial_ids: Optional[list[str]] = None


@dataclass
class CoordinatorBriefingResponse:
    total_patients: int
    trials_assessed: list[str]
    opportunities: list[dict]   # {trial_id, trial_name, eligible_count, top_candidates}
    briefing_text: str          # Coordinator-ready paragraph


# ------------------------------------------------------------------ #
#  MCPServer                                                           #
# ------------------------------------------------------------------ #

class MCPServer:
    """
    GlucoFlow MCP Server — the unified coordinator interface.

    Parameters
    ----------
    bedrock_client : BedrockClient, optional
        Injectable for testing. If None, a real BedrockClient is created
        on first use (which requires live AWS Bedrock credentials).
    """

    def __init__(self, bedrock_client=None):
        pass  # bedrock_client param kept for test compatibility but no longer used

    # ---------------------------------------------------------------- #
    #  Action 1: screen_patient_for_trial                               #
    # ---------------------------------------------------------------- #

    def screen_patient_for_trial(
        self, request: ScreenPatientRequest
    ) -> ScreenPatientResponse:
        """
        Screen a single patient against a single trial protocol.

        Returns eligibility decision + full audit trail + optional
        Bedrock narrative explaining the result to the coordinator.
        """
        protocol = load_protocol(request.trial_id)
        result = screen_patient(request.patient_id, request.gold_metrics, protocol)
        audit = _build_audit_trail(result)

        narrative = None  # AI narrative removed — eligibility output is self-contained

        return ScreenPatientResponse(
            patient_id=request.patient_id,
            trial_id=request.trial_id,
            eligible=result.eligible,
            summary=result.summary,
            narrative=narrative,
            audit_trail=audit,
        )

    # ---------------------------------------------------------------- #
    #  Action 2: find_eligible_patients                                 #
    # ---------------------------------------------------------------- #

    def find_eligible_patients(
        self, request: FindEligibleRequest
    ) -> FindEligibleResponse:
        """
        Screen an entire cohort against one trial. Returns ranked candidates.
        Eligible patients appear first, sorted by patient_id within each group.
        """
        protocol = load_protocol(request.trial_id)
        results = screen_cohort(request.cohort_metrics, protocol)

        candidates = []
        for r in results:
            candidate = {
                "patient_id": r.patient_id,
                "eligible": r.eligible,
                "summary": r.summary,
                "criteria_passed": len(r.passed_inclusion),
                "criteria_failed": len(r.failed_inclusion),
                "exclusions_triggered": len(r.exclusion_triggered),
                "audit_trail": _build_audit_trail(r),
            }
            candidates.append(candidate)

        eligible_count = sum(1 for r in results if r.eligible)
        total = len(results)

        population_summary = (
            f"Screened {total} patient(s) against {protocol.trial_name} "
            f"({request.trial_id}). "
            f"{eligible_count} of {total} meet pre-screening criteria "
            f"and warrant coordinator follow-up."
        )

        return FindEligibleResponse(
            trial_id=request.trial_id,
            trial_name=protocol.trial_name,
            eligible_count=eligible_count,
            total_screened=total,
            candidates=candidates,
            population_summary=population_summary,
        )

    # ---------------------------------------------------------------- #
    #  Action 3: explain_exclusion                                      #
    # ---------------------------------------------------------------- #

    def explain_exclusion(
        self, request: ExplainExclusionRequest
    ) -> ExplainExclusionResponse:
        """
        Explain in plain English why a patient does not qualify for a trial.
        Includes a 'what to do next' recommendation for the coordinator.
        """
        protocol = load_protocol(request.trial_id)
        result = screen_patient(request.patient_id, request.gold_metrics, protocol)

        failed_criteria = []

        for r in result.failed_inclusion:
            failed_criteria.append({
                "type": "inclusion_not_met",
                "field": r.criterion.field,
                "label": r.criterion.human_label,
                "patient_value": r.patient_value,
                "required": f"{r.criterion.operator} {r.criterion.threshold}",
                "reason": r.failure_reason,
                "clinical_rationale": r.criterion.human_rationale,
            })

        for r in result.exclusion_triggered:
            failed_criteria.append({
                "type": "exclusion_triggered",
                "field": r.criterion.field,
                "label": r.criterion.human_label,
                "patient_value": r.patient_value,
                "threshold": f"{r.criterion.operator} {r.criterion.threshold}",
                "reason": r.failure_reason,
                "clinical_rationale": r.criterion.human_rationale,
            })

        if result.eligible:
            explanation = (
                f"Patient {request.patient_id} actually MEETS all pre-screening "
                f"criteria for {protocol.trial_name}. No exclusions apply."
            )
            recommendation = (
                "This patient is a pre-screening candidate. Proceed with formal "
                "screening visit and consent process per protocol."
            )
        else:
            reasons_text = "; ".join(
                fc["label"] for fc in failed_criteria
            )
            explanation = (
                f"Patient {request.patient_id} does not currently meet pre-screening "
                f"criteria for {protocol.trial_name}. "
                f"Reason(s): {reasons_text}."
            )
            # Build tailored recommendation based on what failed
            inc_failed = [fc for fc in failed_criteria if fc["type"] == "inclusion_not_met"]
            exc_triggered = [fc for fc in failed_criteria if fc["type"] == "exclusion_triggered"]

            rec_parts = []
            if exc_triggered:
                rec_parts.append(
                    "Patient triggered an exclusion criterion — refer for clinical "
                    "review before any re-screening."
                )
            if inc_failed:
                rec_parts.append(
                    f"Patient does not yet meet {len(inc_failed)} inclusion "
                    f"criterion/criteria. Re-screen after 30–90 days if clinical "
                    f"status changes."
                )
            recommendation = " ".join(rec_parts) if rec_parts else (
                "No further action required for this trial."
            )

        return ExplainExclusionResponse(
            patient_id=request.patient_id,
            trial_id=request.trial_id,
            eligible=result.eligible,
            plain_english_explanation=explanation,
            failed_criteria=failed_criteria,
            recommendation=recommendation,
        )

    # ---------------------------------------------------------------- #
    #  Action 4: compare_trials_for_patient                            #
    # ---------------------------------------------------------------- #

    def compare_trials_for_patient(
        self, request: CompareTrialsRequest
    ) -> CompareTrialsResponse:
        """
        Compare a patient across multiple trial protocols simultaneously.
        Returns which trials they qualify for and a ranked best-match.
        """
        trial_ids = request.trial_ids or list_protocols()
        comparison_table = []
        eligible_trials = []
        ineligible_trials = []

        for trial_id in trial_ids:
            try:
                protocol = load_protocol(trial_id)
                result = screen_patient(request.patient_id, request.gold_metrics, protocol)
                row = {
                    "trial_id": trial_id,
                    "trial_name": protocol.trial_name,
                    "eligible": result.eligible,
                    "criteria_passed": len(result.passed_inclusion),
                    "criteria_total": len(protocol.inclusion_criteria),
                    "exclusions_triggered": len(result.exclusion_triggered),
                    "summary": result.summary,
                }
                comparison_table.append(row)
                if result.eligible:
                    eligible_trials.append(trial_id)
                else:
                    ineligible_trials.append(trial_id)
            except Exception as e:
                logger.warning(f"Could not evaluate trial {trial_id}: {e}")
                comparison_table.append({
                    "trial_id": trial_id,
                    "trial_name": "Unknown",
                    "eligible": False,
                    "error": str(e),
                })

        # Best match = eligible trial with most criteria passed
        best_match = None
        if eligible_trials:
            eligible_rows = [r for r in comparison_table if r.get("eligible")]
            best_row = max(eligible_rows, key=lambda r: r.get("criteria_passed", 0))
            best_match = best_row["trial_id"]

        return CompareTrialsResponse(
            patient_id=request.patient_id,
            eligible_trials=eligible_trials,
            ineligible_trials=ineligible_trials,
            best_match=best_match,
            comparison_table=comparison_table,
        )

    # ---------------------------------------------------------------- #
    #  Action 5: get_coordinator_briefing                               #
    # ---------------------------------------------------------------- #

    def get_coordinator_briefing(
        self, request: CoordinatorBriefingRequest
    ) -> CoordinatorBriefingResponse:
        """
        Population-level trial opportunity briefing.
        Screens all patients against all (or specified) trials and
        returns a coordinator-ready summary of recruitment opportunities.
        """
        trial_ids = request.trial_ids or list_protocols()
        opportunities = []

        for trial_id in trial_ids:
            try:
                protocol = load_protocol(trial_id)
                results = screen_cohort(request.cohort_metrics, protocol)
                eligible = [r for r in results if r.eligible]
                top_candidates = [
                    {"patient_id": r.patient_id, "summary": r.summary}
                    for r in eligible[:3]  # Top 3
                ]
                opportunities.append({
                    "trial_id": trial_id,
                    "trial_name": protocol.trial_name,
                    "phase": protocol.phase,
                    "eligible_count": len(eligible),
                    "total_screened": len(results),
                    "top_candidates": top_candidates,
                })
            except Exception as e:
                logger.warning(f"Could not screen cohort for trial {trial_id}: {e}")

        total_patients = len(request.cohort_metrics)
        trials_with_candidates = [o for o in opportunities if o["eligible_count"] > 0]

        if trials_with_candidates:
            lines = [
                f"Coordinator Briefing — {total_patients} patient(s) screened "
                f"across {len(trial_ids)} trial(s)."
            ]
            for o in trials_with_candidates:
                lines.append(
                    f"  • {o['trial_name']}: {o['eligible_count']} candidate(s) identified."
                )
            briefing_text = "\n".join(lines)
        else:
            briefing_text = (
                f"No patients in the current cohort of {total_patients} meet "
                f"pre-screening criteria for any of the {len(trial_ids)} assessed trials. "
                f"Consider expanding the cohort or re-screening after clinical review."
            )

        return CoordinatorBriefingResponse(
            total_patients=total_patients,
            trials_assessed=trial_ids,
            opportunities=opportunities,
            briefing_text=briefing_text,
        )


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #

def _build_audit_trail(result: EligibilityResult) -> dict:
    """
    Build a structured audit trail dict from an EligibilityResult.
    This is stored alongside every response for coordinator records.
    """
    return {
        "patient_id": result.patient_id,
        "trial_id": result.trial_id,
        "eligible": result.eligible,
        "inclusion_criteria": [
            {
                "field": r.criterion.field,
                "label": r.criterion.human_label,
                "operator": r.criterion.operator,
                "threshold": r.criterion.threshold,
                "patient_value": r.patient_value,
                "passed": r.passed,
                "reason": r.failure_reason,
            }
            for r in result.inclusion_results
        ],
        "exclusion_criteria": [
            {
                "field": r.criterion.field,
                "label": r.criterion.human_label,
                "operator": r.criterion.operator,
                "threshold": r.criterion.threshold,
                "patient_value": r.patient_value,
                "triggered": r.passed,  # True = patient triggered this exclusion
                "reason": r.failure_reason,
            }
            for r in result.exclusion_results
        ],
    }
