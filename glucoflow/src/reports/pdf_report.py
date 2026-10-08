"""
pdf_report.py — GlucoFlow Pre-Screening Coordinator Report Generator
=====================================================================

Generates a structured plaintext (.txt) pre-screening report from an
EligibilityResult.  No third-party PDF dependencies — the output is a
professionally-formatted ASCII text file suitable for email, printing,
or electronic study binders.

Key public API
--------------
generate_prescreening_report(result, screening_date) -> str
    Build and return the full report as a string.

save_report(result, output_path, screening_date) -> Path
    Write the report to *output_path* and return the resolved Path.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from src.trials.eligibility import CriterionResult, EligibilityResult

# ------------------------------------------------------------------ #
#  Layout constants                                                    #
# ------------------------------------------------------------------ #

_PAGE_WIDTH    = 78
_SECTION_WIDTH = 42   # width of the criterion description column
_DIVIDER_HEAVY = "=" * _PAGE_WIDTH
_DIVIDER_LIGHT = "-" * _PAGE_WIDTH


# ------------------------------------------------------------------ #
#  Internal helpers                                                    #
# ------------------------------------------------------------------ #

def _centre(text: str, width: int = _PAGE_WIDTH) -> str:
    """Return *text* centre-aligned within *width* characters."""
    return text.center(width)


def _section_header(title: str) -> str:
    """Return a light-divider line followed by a section title."""
    return f"\n  {title}\n  {'-' * 40}"


def _criterion_line(cr: CriterionResult, criterion_type: str) -> str:
    """
    Format a single criterion result as a fixed-width report line.

    Parameters
    ----------
    cr             : CriterionResult from EligibilityEngine
    criterion_type : 'inclusion' or 'exclusion'
    """
    c = cr.criterion

    # Build human-readable threshold string
    if c.threshold_str is not None:
        threshold_repr = f"'{c.threshold_str}'"
    elif c.threshold is not None:
        threshold_repr = str(c.threshold)
    else:
        threshold_repr = "?"

    # Use human_label when available for a readable description; fall back
    # to the raw field + operator + threshold expression.
    if c.human_label:
        description = c.human_label
    else:
        description = f"{c.field} {c.operator} {threshold_repr}"

    # Patient value
    pv = cr.patient_value
    if pv is None:
        pv_str = "(missing)"
    elif isinstance(pv, bool):
        pv_str = str(pv)
    elif isinstance(pv, float) and pv == int(pv):
        pv_str = str(int(pv))
    else:
        pv_str = str(pv)

    # Pass/fail badge — semantics differ for inclusion vs exclusion:
    #   inclusion: passed=True  → criterion met        → PASS ✓
    #              passed=False → criterion NOT met    → FAIL ✗
    #   exclusion: passed=True  → exclusion TRIGGERED  → EXCL  ✗  (bad)
    #              passed=False → exclusion not met    → CLEAR ✓  (good)
    if criterion_type == "exclusion":
        if cr.passed:
            badge       = "[EXCL]  ✗"        # triggered — patient excluded
            status_note = "  ← EXCLUSION TRIGGERED"
        else:
            badge       = "[CLEAR] ✓"        # not triggered — patient passes gate
            status_note = ""
    else:  # inclusion
        if cr.passed:
            badge       = "[PASS]  ✓"
            status_note = ""
        else:
            badge       = "[FAIL]  ✗"
            status_note = "  ← INCLUSION NOT MET"

    return f"  {badge:<11} {description:<40} Value: {pv_str}{status_note}"


def _wrap_text(text: str, indent: int = 2) -> str:
    """
    Wrap long text to *_PAGE_WIDTH* characters with a left indent.

    Uses a simple word-wrap that honours newlines already in *text*.
    """
    words  = text.split()
    lines  = []
    line   = " " * indent
    for word in words:
        if len(line) + len(word) + 1 > _PAGE_WIDTH:
            lines.append(line.rstrip())
            line = " " * indent + word + " "
        else:
            line += word + " "
    if line.strip():
        lines.append(line.rstrip())
    return "\n".join(lines)


# ------------------------------------------------------------------ #
#  Verdict banner                                                      #
# ------------------------------------------------------------------ #

def _verdict_banner(eligible: bool) -> str:
    """Return a visually prominent eligibility verdict block."""
    if eligible:
        inner = "  ✓  ELIGIBLE — CANDIDATE     "
        box   = [
            "  ╔══════════════════════════════╗",
            "  ║" + inner + "║",
            "  ╚══════════════════════════════╝",
        ]
    else:
        inner = "  ✗  NOT ELIGIBLE             "
        box   = [
            "  ╔══════════════════════════════╗",
            "  ║" + inner + "║",
            "  ╚══════════════════════════════╝",
        ]
    # Centre the box
    centred = "\n".join(_centre(line) for line in box)
    return centred


# ------------------------------------------------------------------ #
#  Summary sentence                                                    #
# ------------------------------------------------------------------ #

def _summary_sentence(result: EligibilityResult) -> str:
    """One-sentence coordinator-readable summary."""
    pid   = result.patient_id
    tid   = result.trial_id.upper()
    n_inc = len(result.inclusion_results)
    n_ok  = sum(1 for r in result.inclusion_results if r.passed)
    n_exc = len([r for r in result.exclusion_results if r.passed])

    if result.eligible:
        return (
            f"Patient {pid} is a PRE-SCREENING CANDIDATE for {tid}. "
            f"All {n_ok} inclusion criteria satisfied. "
            f"No exclusion criteria triggered."
        )
    else:
        reasons = []
        if n_ok < n_inc:
            reasons.append(f"{n_inc - n_ok} inclusion criterion/criteria not met")
        if n_exc:
            reasons.append(f"{n_exc} exclusion criterion/criteria triggered")
        reason_str = "; ".join(reasons) or "see details below"
        return (
            f"Patient {pid} does NOT qualify for {tid} pre-screening. "
            f"Reason(s): {reason_str}."
        )


# ------------------------------------------------------------------ #
#  Main report builder                                                 #
# ------------------------------------------------------------------ #

def generate_prescreening_report(
    result: EligibilityResult,
    screening_date: date,
) -> str:
    """
    Generate a structured pre-screening coordinator report.

    Parameters
    ----------
    result         : EligibilityResult from EligibilityEngine.screen_patient()
    screening_date : The calendar date on which the screening was run.

    Returns
    -------
    str — multi-line ASCII report text ready for .txt file or display.
    """
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines: list[str] = []

    # ── Header ──────────────────────────────────────────────────────
    lines.append(_DIVIDER_HEAVY)
    lines.append(_centre("CLINICAL TRIAL PRE-SCREENING REPORT"))
    lines.append(_centre("GlucoFlow v2 Pre-Screening Engine"))
    lines.append(_DIVIDER_HEAVY)
    lines.append("")
    lines.append(f"  {'Patient ID:':<24} {result.patient_id}")
    lines.append(f"  {'Screening Date:':<24} {screening_date.isoformat()}")
    lines.append(f"  {'Trial ID:':<24} {result.trial_id.upper()}")
    lines.append(f"  {'Report Generated:':<24} {now_str}")
    lines.append("")
    lines.append(_DIVIDER_LIGHT)

    # ── Verdict banner ───────────────────────────────────────────────
    lines.append("")
    lines.append(_verdict_banner(result.eligible))
    lines.append("")
    lines.append(
        _wrap_text(f"Summary:               {_summary_sentence(result)}")
    )
    lines.append("")
    lines.append(_DIVIDER_LIGHT)

    # ── Inclusion criteria ───────────────────────────────────────────
    lines.append(_section_header("INCLUSION CRITERIA"))
    if result.inclusion_results:
        for cr in result.inclusion_results:
            lines.append(_criterion_line(cr, "inclusion"))
    else:
        lines.append("  (no inclusion criteria defined)")

    lines.append("")
    lines.append(_DIVIDER_LIGHT)

    # ── Exclusion criteria ───────────────────────────────────────────
    lines.append(_section_header("EXCLUSION CRITERIA"))
    if result.exclusion_results:
        for cr in result.exclusion_results:
            lines.append(_criterion_line(cr, "exclusion"))
    else:
        lines.append("  (no exclusion criteria defined)")

    lines.append("")
    lines.append(_DIVIDER_LIGHT)

    # ── Coordinator note ─────────────────────────────────────────────
    lines.append(_section_header("NOTE TO COORDINATOR"))
    disclaimer = (
        "IMPORTANT: This report is an automated pre-screening aide only. "
        "It does NOT constitute a clinical eligibility determination. "
        "All inclusion/exclusion decisions must be confirmed by a qualified "
        "clinical investigator before study enrolment. GlucoFlow provides no "
        "warranty of medical fitness."
    )
    lines.append(_wrap_text(disclaimer))
    lines.append("")
    lines.append(_DIVIDER_HEAVY)
    lines.append("")

    return "\n".join(lines)


# ------------------------------------------------------------------ #
#  File saver                                                          #
# ------------------------------------------------------------------ #

def save_report(
    result: EligibilityResult,
    output_path: str | Path,
    screening_date: date,
) -> Path:
    """
    Write the pre-screening report to a .txt file.

    Parameters
    ----------
    result        : EligibilityResult from EligibilityEngine.screen_patient()
    output_path   : Destination file path (will be created, parents made).
    screening_date : the screening date to embed in the report

    Returns
    -------
    Path — the resolved path of the written file.
    """
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    report_text = generate_prescreening_report(
        result=result,
        screening_date=screening_date,
    )
    path.write_text(report_text, encoding="utf-8")
    return path
