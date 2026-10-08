"""
eligibility.py — GlucoFlow Clinical Trial Eligibility Engine

Evaluates a patient's Gold-layer daily metrics against a TrialProtocol
and returns a fully-structured, auditable eligibility decision.

Design Principles:
  - Every decision is traceable: the EligibilityResult records which
    criteria passed, which failed, and the exact patient metric value
    that caused each outcome. No black boxes.
  - The engine never makes a clinical decision — it produces a
    pre-screening flag. A coordinator or clinician always makes the
    final call. This is legally and ethically analogous to how
    TrialSpark, TriNetX, and Medidata Rave work commercially.
  - Pure functions throughout — evaluate_criterion() and
    screen_patient() have no side effects and are independently
    testable without any AWS calls.
  - Operator evaluation is centralised in _apply_operator() so the
    comparison logic is in exactly one place.

Clinical Context:
  Inclusion criteria: patient MUST satisfy ALL of them to be eligible.
  Exclusion criteria: patient must satisfy NONE of them to be eligible.
    (A patient who "meets" an exclusion criterion is excluded.)

Iteration Path:
  v1 (current): CGM metrics only — evaluates against Gold layer fields
  v2 (done):     + Clinical fields: diabetes_type, age, duration_years,
                   has_retinopathy, has_nephropathy, has_neuropathy
  v3 (planned):  + CDISC SDTM mapping for regulatory-grade audit trails
"""

import operator as op
from dataclasses import dataclass, field
from typing import Optional

from src.trials.protocol import TrialCriterion, TrialProtocol

# ------------------------------------------------------------------ #
#  Operator map                                                        #
# ------------------------------------------------------------------ #

_OPERATORS = {
    ">=": op.ge,
    "<=": op.le,
    ">":  op.gt,
    "<":  op.lt,
    "==": op.eq,
    "!=": op.ne,
}


def _apply_operator(value: float, operator: str, threshold: float) -> bool:
    """
    Apply a comparison operator between a patient metric value and a threshold.

    Parameters
    ----------
    value     : float — the patient's actual metric
    operator  : str   — one of >=, <=, >, <, ==, !=
    threshold : float — the protocol's threshold value

    Returns
    -------
    bool — True if the comparison holds, False otherwise.
    """
    fn = _OPERATORS.get(operator)
    if fn is None:
        raise ValueError(f"Unsupported operator: '{operator}'")
    return fn(value, threshold)


def _apply_operator_str(value: str, operator: str, threshold_str: str) -> bool:
    """
    Apply == / != comparison between a patient's string field and a threshold.

    Only == and != are meaningful for string fields; other operators raise
    ValueError to catch misconfigured protocols at evaluation time.

    Parameters
    ----------
    value         : str — the patient's categorical value (e.g. 'T2DM')
    operator      : str — must be '==' or '!='
    threshold_str : str — the expected string value in the protocol
    """
    if operator == "==":
        return value == threshold_str
    if operator == "!=":
        return value != threshold_str
    raise ValueError(
        f"Operator '{operator}' is not supported for string fields. "
        f"Use '==' or '!='."
    )


# ------------------------------------------------------------------ #
#  Criterion result                                                    #
# ------------------------------------------------------------------ #

@dataclass
class CriterionResult:
    """
    The outcome of evaluating a single TrialCriterion against a patient.

    Attributes
    ----------
    criterion       : the criterion that was evaluated
    patient_value   : the patient's actual metric value (None if missing)
    passed          : True if the patient satisfies this criterion
    failure_reason  : human-readable explanation if passed=False
    """
    criterion: TrialCriterion
    patient_value: Optional[float]
    passed: bool
    failure_reason: Optional[str] = None


# ------------------------------------------------------------------ #
#  Eligibility result                                                  #
# ------------------------------------------------------------------ #

@dataclass
class EligibilityResult:
    """
    The complete pre-screening outcome for one patient against one trial.

    Attributes
    ----------
    patient_id          : patient identifier
    trial_id            : trial identifier
    eligible            : True only if ALL inclusion criteria pass AND
                          NO exclusion criteria are triggered
    inclusion_results   : per-criterion outcomes for inclusion criteria
    exclusion_results   : per-criterion outcomes for exclusion criteria
    exclusion_triggered : list of exclusion criteria the patient triggered
    summary             : one-line human-readable eligibility summary
    """
    patient_id:           str
    trial_id:             str
    eligible:             bool
    inclusion_results:    list[CriterionResult]
    exclusion_results:    list[CriterionResult]
    exclusion_triggered:  list[CriterionResult]
    summary:              str

    # Convenience properties
    @property
    def failed_inclusion(self) -> list[CriterionResult]:
        """Inclusion criteria the patient did NOT satisfy."""
        return [r for r in self.inclusion_results if not r.passed]

    @property
    def passed_inclusion(self) -> list[CriterionResult]:
        """Inclusion criteria the patient DID satisfy."""
        return [r for r in self.inclusion_results if r.passed]


# ------------------------------------------------------------------ #
#  Core evaluation functions                                           #
# ------------------------------------------------------------------ #

# String-valued clinical fields that use threshold_str for comparison.
# All other fields are coerced to float for numeric comparison.
_STRING_FIELDS = frozenset({"diabetes_type"})

# Bool-valued clinical fields. Patient values are Python booleans;
# they are coerced to float (True→1.0, False→0.0) before comparison.
_BOOL_FIELDS = frozenset({"has_retinopathy", "has_nephropathy", "has_neuropathy"})


def evaluate_criterion(
    criterion: TrialCriterion,
    gold_metrics: dict,
    criterion_type: str = "inclusion",
) -> CriterionResult:
    """
    Evaluate a single criterion against a patient's combined metrics dict.

    v2 extension: handles three field types:
      - Numeric (CGM + age, duration_years): coerces raw value to float,
        compares against criterion.threshold.
      - Bool (has_*): coerces True/False to 1.0/0.0, compares against
        criterion.threshold.
      - String (diabetes_type): compares raw string against
        criterion.threshold_str using the operator.

    Parameters
    ----------
    criterion       : TrialCriterion to evaluate
    gold_metrics    : dict — GoldReading or combined clinical metrics dict
    criterion_type  : "inclusion" or "exclusion" — affects failure messaging

    Returns
    -------
    CriterionResult
    """
    raw_value = gold_metrics.get(criterion.field)

    # Missing metric — cannot evaluate, treat as not-passed
    if raw_value is None:
        return CriterionResult(
            criterion=criterion,
            patient_value=None,
            passed=False,
            failure_reason=(
                f"Metric '{criterion.field}' is unavailable for this patient. "
                f"Cannot confirm {criterion_type} criterion: {criterion.human_label}."
            )
        )

    # --- String comparison path (e.g. diabetes_type == 'T2DM') ---
    if criterion.field in _STRING_FIELDS:
        str_value = str(raw_value)
        if criterion.threshold_str is None:
            # Misconfigured criterion — treat as not-passed
            return CriterionResult(
                criterion=criterion,
                patient_value=None,
                passed=False,
                failure_reason=(
                    f"Criterion '{criterion.human_label}' is a string field but "
                    f"has no threshold_str configured."
                ),
            )
        passed = _apply_operator_str(str_value, criterion.operator, criterion.threshold_str)
        patient_value = None  # no numeric patient_value for string fields
        failure_reason = None
        if not passed:
            if criterion_type == "inclusion":
                failure_reason = (
                    f"Patient {criterion.field} = '{str_value}' does not satisfy "
                    f"{criterion.operator} '{criterion.threshold_str}' "
                    f"({criterion.human_label}). "
                    f"Rationale: {criterion.human_rationale}"
                )
            else:
                failure_reason = (
                    f"Patient {criterion.field} = '{str_value}' triggers "
                    f"exclusion criterion {criterion.operator} '{criterion.threshold_str}' "
                    f"({criterion.human_label}). "
                    f"Rationale: {criterion.human_rationale}"
                )
        return CriterionResult(
            criterion=criterion,
            patient_value=patient_value,
            passed=passed,
            failure_reason=failure_reason,
        )

    # --- Numeric / bool comparison path ---
    # Bool fields: coerce Python True/False to 1.0/0.0
    if criterion.field in _BOOL_FIELDS:
        patient_value = 1.0 if raw_value else 0.0
    else:
        patient_value = float(raw_value)

    passed = _apply_operator(patient_value, criterion.operator, criterion.threshold)

    failure_reason = None
    if not passed:
        if criterion_type == "inclusion":
            failure_reason = (
                f"Patient {criterion.field} = {patient_value:.2f} does not satisfy "
                f"{criterion.operator} {criterion.threshold} "
                f"({criterion.human_label}). "
                f"Rationale: {criterion.human_rationale}"
            )
        else:
            # For exclusion criteria, "passed" means the patient triggers the exclusion
            failure_reason = (
                f"Patient {criterion.field} = {patient_value:.2f} triggers "
                f"exclusion criterion {criterion.operator} {criterion.threshold} "
                f"({criterion.human_label}). "
                f"Rationale: {criterion.human_rationale}"
            )

    return CriterionResult(
        criterion=criterion,
        patient_value=patient_value,
        passed=passed,
        failure_reason=failure_reason,
    )


def screen_patient(
    patient_id: str,
    gold_metrics: dict,
    protocol: TrialProtocol,
) -> EligibilityResult:
    """
    Screen a patient against a full trial protocol.

    Evaluation logic:
      1. Evaluate every inclusion criterion. Patient must pass ALL.
      2. Evaluate every exclusion criterion. Patient must trigger NONE.
      3. eligible = all_inclusion_passed AND no_exclusion_triggered

    Parameters
    ----------
    patient_id   : str  — patient identifier (for the result record)
    gold_metrics : dict — GoldReading serialised as model_dump(mode="json")
    protocol     : TrialProtocol — the trial to screen against

    Returns
    -------
    EligibilityResult — fully structured, auditable pre-screening outcome
    """
    # --- Evaluate inclusion criteria ---
    inclusion_results = [
        evaluate_criterion(c, gold_metrics, criterion_type="inclusion")
        for c in protocol.inclusion_criteria
    ]
    all_inclusion_passed = all(r.passed for r in inclusion_results)

    # --- Evaluate exclusion criteria ---
    # For exclusion: _apply_operator returning True means the patient
    # MEETS the exclusion condition — so they are excluded.
    exclusion_results = [
        evaluate_criterion(c, gold_metrics, criterion_type="exclusion")
        for c in protocol.exclusion_criteria
    ]
    exclusion_triggered = [r for r in exclusion_results if r.passed]
    no_exclusion_triggered = len(exclusion_triggered) == 0

    # --- Final eligibility ---
    eligible = all_inclusion_passed and no_exclusion_triggered

    # --- Build summary ---
    if eligible:
        summary = (
            f"Patient {patient_id} is a PRE-SCREENING CANDIDATE for "
            f"{protocol.trial_name} ({protocol.trial_id}). "
            f"All {len(inclusion_results)} inclusion criteria satisfied. "
            f"No exclusion criteria triggered."
        )
    else:
        reasons = []
        failed_inc = [r for r in inclusion_results if not r.passed]
        if failed_inc:
            reasons.append(
                f"{len(failed_inc)} inclusion criterion/criteria not met: "
                + "; ".join(r.criterion.human_label for r in failed_inc)
            )
        if exclusion_triggered:
            reasons.append(
                f"{len(exclusion_triggered)} exclusion criterion/criteria triggered: "
                + "; ".join(r.criterion.human_label for r in exclusion_triggered)
            )
        summary = (
            f"Patient {patient_id} does NOT meet pre-screening criteria for "
            f"{protocol.trial_name} ({protocol.trial_id}). "
            + " | ".join(reasons)
        )

    return EligibilityResult(
        patient_id=patient_id,
        trial_id=protocol.trial_id,
        eligible=eligible,
        inclusion_results=inclusion_results,
        exclusion_results=exclusion_results,
        exclusion_triggered=exclusion_triggered,
        summary=summary,
    )


# ------------------------------------------------------------------ #
#  Batch screener                                                      #
# ------------------------------------------------------------------ #

def screen_cohort(
    gold_metrics_list: list[dict],
    protocol: TrialProtocol,
) -> list[EligibilityResult]:
    """
    Screen an entire cohort (list of Gold metric dicts) against one protocol.

    Parameters
    ----------
    gold_metrics_list : list of GoldReading model_dump(mode="json") dicts
    protocol          : TrialProtocol to screen against

    Returns
    -------
    list[EligibilityResult] — one result per patient, eligible patients first,
    then ineligible, sorted by patient_id within each group.
    """
    results = [
        screen_patient(m.get("patient_id", "unknown"), m, protocol)
        for m in gold_metrics_list
    ]
    # Sort: eligible first, then by patient_id
    return sorted(results, key=lambda r: (not r.eligible, r.patient_id))
