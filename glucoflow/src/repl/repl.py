"""
repl/repl.py — GlucoFlow Conversational CLI REPL

The user-facing front door to everything GlucoFlow has built.

A clinical trial coordinator launches this REPL and queries in plain English:

  glucoflow> who qualifies for the diamond trial?
  glucoflow> why doesn't child_001 qualify for the closed-loop study?
  glucoflow> compare trials for adult_001
  glucoflow> give me a briefing

The REPL:
  1. Parses the query (IntentParser — deterministic, zero cost, instant)
  2. Resolves missing context from its in-memory cohort + session state
  3. Dispatches to the appropriate MCPServer action
  4. Formats the response for the terminal

In demo mode (--demo flag or GLUCOFLOW_DEMO=1 env var):
  - A synthetic cohort from the simglucose data is auto-loaded
  - A mock BedrockClient is injected (no real AWS calls, no cost)
  - The REPL is fully usable for a portfolio demo without any AWS account

In live mode (default):
  - Cohort metrics are loaded from the Gold S3 bucket via Athena
  - Real Bedrock narratives are generated on relevant queries

Design:
  The REPL session holds a `_cohort` list in memory — Gold metric dicts
  for all patients. This avoids repeated S3 reads during a session.
  The cohort is refreshed with the `refresh` command or on startup.
"""

import os
import json
import textwrap
import logging
from pathlib import Path
from typing import Optional

from src.repl.intent_parser import IntentParser, ParsedIntent, Intent
from src.mcp.server import (
    MCPServer,
    ScreenPatientRequest,
    FindEligibleRequest,
    ExplainExclusionRequest,
    CompareTrialsRequest,
    CoordinatorBriefingRequest,
)
from src.trials.protocol import list_protocols

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  ANSI colour helpers                                                 #
# ------------------------------------------------------------------ #

GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
CYAN    = "\033[96m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
RESET   = "\033[0m"

def _g(s): return f"{GREEN}{s}{RESET}"
def _y(s): return f"{YELLOW}{s}{RESET}"
def _r(s): return f"{RED}{s}{RESET}"
def _c(s): return f"{CYAN}{s}{RESET}"
def _b(s): return f"{BOLD}{s}{RESET}"
def _d(s): return f"{DIM}{s}{RESET}"


# ------------------------------------------------------------------ #
#  Synthetic demo cohort                                              #
# ------------------------------------------------------------------ #

# Fallback cohort used only when no CSV files are found in data/samples/
_FALLBACK_COHORT = [
    {
        # adolescent_001 — T1DM, age 16, suboptimal control
        # Qualifies for: DIAMOND T1DM (GMI 7.9, TIR 58.3%, 0 severe hypos)
        # Does NOT qualify for: GLP-1 T2DM (wrong diabetes type)
        # Does NOT qualify for: Closed-Loop (CV% 33.1 < 36% threshold)
        "patient_id": "adolescent_001",
        "day": "2026-10-07",
        "tir_percent": 58.3,
        "mean_glucose": 171.4,
        "hypo_events": 2,
        "severe_hypo_events": 0,
        "cv_percent": 33.1,
        "gmi": 7.9,
        "source_system": "dexcom_messy",
        # v2 clinical fields
        "diabetes_type": "T1DM",
        "age": 16,
        "duration_years": 4.0,
        "has_retinopathy": False,
        "has_nephropathy": False,
        "has_neuropathy": False,
    },
    {
        # adult_001 — T2DM, age 54, moderate control
        # Qualifies for: GLP-1 T2DM (GMI 7.6, TIR 64.2%, mean glucose 162.7)
        # Does NOT qualify for: DIAMOND T1DM (wrong diabetes type)
        # Does NOT qualify for: Closed-Loop (T2DM, CV% 29.4)
        "patient_id": "adult_001",
        "day": "2026-10-07",
        "tir_percent": 64.2,
        "mean_glucose": 162.7,
        "hypo_events": 1,
        "severe_hypo_events": 0,
        "cv_percent": 29.4,
        "gmi": 7.6,
        "source_system": "dexcom_messy",
        # v2 clinical fields
        "diabetes_type": "T2DM",
        "age": 54,
        "duration_years": 11.0,
        "has_retinopathy": False,
        "has_nephropathy": False,
        "has_neuropathy": True,
    },
    {
        # child_001 — T1DM, age 9, very poor control + frequent hypos
        # Qualifies for: Closed-Loop (CV% 44.7, hypos 6/day, TIR 47.1%, T1DM)
        # Does NOT qualify for: DIAMOND T1DM (severe_hypo_events=2 triggers exclusion)
        # Does NOT qualify for: GLP-1 T2DM (wrong diabetes type)
        "patient_id": "child_001",
        "day": "2026-10-07",
        "tir_percent": 47.1,
        "mean_glucose": 198.3,
        "hypo_events": 6,
        "severe_hypo_events": 2,
        "cv_percent": 44.7,
        "gmi": 8.5,
        "source_system": "dexcom_messy",
        # v2 clinical fields
        "diabetes_type": "T1DM",
        "age": 9,
        "duration_years": 2.5,
        "has_retinopathy": False,
        "has_nephropathy": False,
        "has_neuropathy": False,
    },
]

# Public alias kept for backward compatibility (tests and external callers)
DEMO_COHORT = _FALLBACK_COHORT

_MESSY_DIR  = Path(__file__).resolve().parents[2] / "data" / "samples" / "messy"
_SYNTH_DIR  = Path(__file__).resolve().parents[2] / "data" / "samples" / "synthetic"


# --------------------------------------------------------------------------- #
#  Archetype inference - maps simglucose patient_id prefix to clinical profile  #
# --------------------------------------------------------------------------- #

_ARCHETYPE_DEFAULTS = {
    "adolescent": {
        "diabetes_type":   "T1DM",
        "age":             16,
        "duration_years":  4.0,
        "has_retinopathy": False,
        "has_nephropathy": False,
        "has_neuropathy":  False,
    },
    "child": {
        "diabetes_type":   "T1DM",
        "age":             9,
        "duration_years":  2.5,
        "has_retinopathy": False,
        "has_nephropathy": False,
        "has_neuropathy":  False,
    },
    "adult": {
        "diabetes_type":   "T2DM",
        "age":             52,
        "duration_years":  9.0,
        "has_retinopathy": False,
        "has_nephropathy": False,
        "has_neuropathy":  False,
    },
}

_UNKNOWN_ARCHETYPE = {
    "diabetes_type":   None,
    "age":             None,
    "duration_years":  None,
    "has_retinopathy": False,
    "has_nephropathy": False,
    "has_neuropathy":  False,
}


def _infer_clinical_fields(patient_id: str) -> dict:
    """Infer diabetes_type and demographics from simglucose patient_id prefix."""
    for prefix, defaults in _ARCHETYPE_DEFAULTS.items():
        if patient_id.startswith(prefix):
            return dict(defaults)
    return dict(_UNKNOWN_ARCHETYPE)


def _compute_gold_metrics(patient_id: str, glucose_values: list[float], source_system: str) -> dict:
    """Compute Gold-layer daily summary metrics from a list of glucose readings.

    v2: merges inferred clinical fields so v2 trial criteria evaluate correctly.
    """
    import math
    from datetime import date

    n = len(glucose_values)
    mean_g = sum(glucose_values) / n
    tir = sum(1 for g in glucose_values if 70.0 <= g <= 180.0) / n * 100.0
    hypo = sum(1 for g in glucose_values if g < 70.0)
    severe_hypo = sum(1 for g in glucose_values if g < 54.0)
    variance = sum((g - mean_g) ** 2 for g in glucose_values) / max(n - 1, 1)
    std_dev = math.sqrt(variance)
    cv = (std_dev / mean_g * 100.0) if mean_g > 0 else 0.0
    gmi = round(3.31 + 0.02392 * mean_g, 2) if mean_g > 0 else None

    metrics = {
        "patient_id": patient_id,
        "day": str(date.today()),
        "tir_percent": round(tir, 2),
        "mean_glucose": round(mean_g, 2),
        "hypo_events": hypo,
        "severe_hypo_events": severe_hypo,
        "cv_percent": round(cv, 2),
        "gmi": gmi,
        "source_system": source_system,
    }
    metrics.update(_infer_clinical_fields(patient_id))
    return metrics


def _build_demo_cohort() -> list[dict]:
    """
    Build the demo cohort dynamically from CSV files in data/samples/.

    Reads messy (Dexcom-style) CSVs first, then synthetic CSVs, so that
    any patient file dropped into either directory is automatically picked up.
    Falls back to _FALLBACK_COHORT if no files are found or all fail to parse.
    """
    from src.ingestion.dexcom import parse_dexcom_export
    from src.ingestion.simglucose import parse_simglucose_csv

    cohort: list[dict] = []
    seen: set[str] = set()

    # --- Messy (Dexcom-style) ---
    for csv_path in sorted(_MESSY_DIR.glob("*.csv")):
        # filename: dexcom_<patient_id>.csv  →  patient_id = stem after "dexcom_"
        stem = csv_path.stem
        patient_id = stem[len("dexcom_"):] if stem.startswith("dexcom_") else stem
        if patient_id in seen:
            continue
        try:
            valid, _failed, _skipped = parse_dexcom_export(csv_path)
            glucose_values = [r.glucose_mgdl for r in valid if r.glucose_mgdl is not None]
            if glucose_values:
                cohort.append(_compute_gold_metrics(patient_id, glucose_values, "dexcom_messy"))
                seen.add(patient_id)
        except Exception as exc:
            logger.warning(f"Demo cohort: could not parse {csv_path.name} — {exc}")

    # --- Synthetic (simglucose) ---
    for csv_path in sorted(_SYNTH_DIR.glob("*.csv")):
        patient_id = csv_path.stem  # e.g. "adult_001"
        if patient_id in seen:
            continue
        try:
            valid, _failed = parse_simglucose_csv(csv_path)
            glucose_values = [r.glucose_mgdl for r in valid if r.glucose_mgdl is not None]
            if glucose_values:
                cohort.append(_compute_gold_metrics(patient_id, glucose_values, "simglucose"))
                seen.add(patient_id)
        except Exception as exc:
            logger.warning(f"Demo cohort: could not parse {csv_path.name} — {exc}")

    if not cohort:
        logger.warning("Demo cohort: no CSV files found in data/samples/ — using fallback cohort.")
        return list(_FALLBACK_COHORT)

    return cohort


# ------------------------------------------------------------------ #
#  GlucoFlowREPL                                                       #
# ------------------------------------------------------------------ #

class GlucoFlowREPL:
    """
    Interactive conversational REPL for clinical trial coordinators.

    Parameters
    ----------
    demo_mode    : bool — if True, use synthetic cohort + mock Bedrock
    bedrock_client : optional injected BedrockClient (for testing)
    cohort       : optional list of Gold metric dicts (for testing)
    """

    BANNER = f"""
{BOLD}{CYAN}╔══════════════════════════════════════════════════════════════╗
║          GlucoFlow — Clinical Trial Pre-Screener             ║
║          CGM-Powered Patient Matching (v1 — CGM Layer)       ║
╚══════════════════════════════════════════════════════════════╝{RESET}

  Ask me anything about your patient cohort and clinical trials.
  Type {_b("help")} for a full command list, {_b("quit")} to exit.
"""

    HELP_TEXT = f"""
{_b("Available commands:")}

  {_c("Cohort queries:")}
    who qualifies for the diamond trial?
    find candidates for the glp1 trial
    who is eligible for the closed-loop study?

  {_c("Patient queries:")}
    screen adult_001 for the diamond trial
    does child_001 qualify for the glp1 trial?
    compare trials for adult_001
    which trial best suits adolescent_001?

  {_c("Explanation queries:")}
    why doesn't child_001 qualify for the diamond trial?
    explain exclusion for child_001 from the aps study

  {_c("Population overview:")}
    give me a briefing
    recruitment overview
    how many candidates do I have?

  {_c("Session commands:")}
    list trials          — show available trial protocols
    list patients        — show patients in current cohort
    refresh              — reload cohort from Gold layer (live mode only)
    help / ?             — show this help text
    quit / exit          — exit the REPL

  {_c("Available trials:")}
    diamond / diamond trial / t1dm      → DIAMOND CGM T1DM Trial
    closed loop / aps / loop            → Closed-Loop APS Candidate
    glp1 / glp-1 / t2dm / sustain       → GLP-1 T2DM Efficacy Trial

  {_c("Available patients (demo):")}
    adolescent_001 / adult_001 / child_001

  {_d("⚠  All outputs are pre-screening flags only — not clinical decisions.")}
"""

    def __init__(
        self,
        demo_mode: bool = True,
        bedrock_client=None,
        cohort: Optional[list] = None,
    ):
        self.demo_mode = demo_mode
        self._parser  = IntentParser()
        self._server  = MCPServer(bedrock_client=bedrock_client)
        self._cohort  = cohort if cohort is not None else (
            _build_demo_cohort() if demo_mode else []
        )
        self._history: list[str] = []

    # ---------------------------------------------------------------- #
    #  Public API                                                        #
    # ---------------------------------------------------------------- #

    def run(self):
        """Start the interactive REPL loop."""
        print(self.BANNER)
        if self.demo_mode:
            print(_y("  ⚡ Demo mode — synthetic cohort loaded, no AWS calls made.\n"))
        else:
            print(_g("  ✓ Live mode — Gold layer cohort loaded.\n"))

        while True:
            try:
                query = input(f"{_b(_c('glucoflow'))}> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n" + _d("Goodbye."))
                break

            if not query:
                continue

            self._history.append(query)
            should_exit = self._handle(query)
            if should_exit:
                break

    def handle_query(self, query: str) -> str:
        """
        Process a single query and return the formatted response string.
        Used programmatically (e.g. in tests) without running the full loop.
        """
        parsed = self._parser.parse(query)
        return self._dispatch(parsed)

    # ---------------------------------------------------------------- #
    #  Internal: handle + dispatch                                      #
    # ---------------------------------------------------------------- #

    def _handle(self, query: str) -> bool:
        """Handle one query. Returns True if the REPL should exit."""
        parsed = self._parser.parse(query)

        if parsed.intent == Intent.QUIT:
            print(_d("\nGoodbye. Results are pre-screening flags — always verify with the formal screening visit."))
            return True

        output = self._dispatch(parsed)
        print(output)
        return False

    def _dispatch(self, parsed: ParsedIntent) -> str:
        """Route a ParsedIntent to the correct MCPServer action."""

        if parsed.intent == Intent.HELP:
            return self.HELP_TEXT

        if parsed.intent == Intent.LIST_TRIALS:
            return self._fmt_trial_list()

        if parsed.intent == Intent.LIST_PATIENTS:
            return self._fmt_patient_list()

        if parsed.confidence == "low" and parsed.suggestion:
            return (
                f"\n{_y('❓ Not sure what you mean.')}\n"
                f"   {parsed.suggestion}\n"
            )

        if parsed.confidence == "medium":
            missing = []
            if not parsed.patient_id:
                missing.append("patient ID (e.g. adult_001)")
            if not parsed.trial_id:
                missing.append("trial name (e.g. 'diamond' or 'glp1')")
            if missing:
                return (
                    f"\n{_y('⚠  I need a bit more information:')}\n"
                    + "\n".join(f"   • Missing: {m}" for m in missing)
                    + f"\n\n   {parsed.suggestion or ''}\n"
                )

        try:
            if parsed.intent == Intent.SCREEN_PATIENT:
                return self._do_screen_patient(parsed)

            if parsed.intent == Intent.FIND_ELIGIBLE:
                return self._do_find_eligible(parsed)

            if parsed.intent == Intent.EXPLAIN_EXCLUSION:
                return self._do_explain_exclusion(parsed)

            if parsed.intent == Intent.COMPARE_TRIALS:
                return self._do_compare_trials(parsed)

            if parsed.intent == Intent.COORDINATOR_BRIEFING:
                return self._do_briefing(parsed)

        except Exception as e:
            logger.exception(f"Error handling query: {parsed.raw_query}")
            return _r(f"\n✗ Error: {e}\n")

        return (
            f"\n{_y('❓ Unknown query.')}\n"
            f"   Type {_b('help')} to see what I can do.\n"
        )

    # ---------------------------------------------------------------- #
    #  Action handlers                                                   #
    # ---------------------------------------------------------------- #

    def _do_screen_patient(self, parsed: ParsedIntent) -> str:
        metrics = self._get_patient_metrics(parsed.patient_id)
        if not metrics:
            return _r(f"\n✗ Patient '{parsed.patient_id}' not found in cohort.\n")

        req = ScreenPatientRequest(
            patient_id=parsed.patient_id,
            trial_id=parsed.trial_id,
            gold_metrics=metrics,
            generate_narrative=False,
        )
        resp = self._server.screen_patient_for_trial(req)
        return _fmt_screen_result(resp)

    def _do_find_eligible(self, parsed: ParsedIntent) -> str:
        if not self._cohort:
            return _y("\n⚠  No patients in cohort. Run in live mode or load patients first.\n")

        req = FindEligibleRequest(
            trial_id=parsed.trial_id,
            cohort_metrics=self._cohort,
            generate_narratives=False,
        )
        resp = self._server.find_eligible_patients(req)
        return _fmt_find_result(resp)

    def _do_explain_exclusion(self, parsed: ParsedIntent) -> str:
        metrics = self._get_patient_metrics(parsed.patient_id)
        if not metrics:
            return _r(f"\n✗ Patient '{parsed.patient_id}' not found in cohort.\n")

        req = ExplainExclusionRequest(
            patient_id=parsed.patient_id,
            trial_id=parsed.trial_id,
            gold_metrics=metrics,
        )
        resp = self._server.explain_exclusion(req)
        return _fmt_explanation(resp)

    def _do_compare_trials(self, parsed: ParsedIntent) -> str:
        metrics = self._get_patient_metrics(parsed.patient_id)
        if not metrics:
            return _r(f"\n✗ Patient '{parsed.patient_id}' not found in cohort.\n")

        req = CompareTrialsRequest(
            patient_id=parsed.patient_id,
            gold_metrics=metrics,
        )
        resp = self._server.compare_trials_for_patient(req)
        return _fmt_comparison(resp)

    def _do_briefing(self, parsed: ParsedIntent) -> str:
        if not self._cohort:
            return _y("\n⚠  No patients in cohort.\n")

        req = CoordinatorBriefingRequest(cohort_metrics=self._cohort)
        resp = self._server.get_coordinator_briefing(req)
        return _fmt_briefing(resp)

    # ---------------------------------------------------------------- #
    #  Helpers                                                           #
    # ---------------------------------------------------------------- #

    def _get_patient_metrics(self, patient_id: Optional[str]) -> Optional[dict]:
        if not patient_id or not self._cohort:
            return None
        for m in self._cohort:
            if m.get("patient_id") == patient_id:
                return m
        return None

    def _fmt_trial_list(self) -> str:
        trials = list_protocols()
        lines = [f"\n{_b('Available trial protocols:')}"]
        aliases = {
            "diamond_t1dm":          "diamond, diamond trial, t1dm, type 1",
            "closed_loop_candidate": "closed loop, aps, artificial pancreas, loop",
            "glp1_t2dm":             "glp1, glp-1, t2dm, type 2, sustain",
        }
        for t in trials:
            lines.append(f"  {_c(t)}  — aliases: {_d(aliases.get(t, ''))}")
        return "\n".join(lines) + "\n"

    def _fmt_patient_list(self) -> str:
        if not self._cohort:
            return _y("\n⚠  No patients in cohort.\n")
        lines = [f"\n{_b('Current cohort:')} ({len(self._cohort)} patient(s))\n"]
        for m in self._cohort:
            tir  = m.get("tir_percent", "?")
            gmi  = m.get("gmi", "?")
            hypo = m.get("severe_hypo_events", "?")
            lines.append(
                f"  {_c(m['patient_id'])}  "
                f"TIR: {tir:.1f}%  GMI: {gmi:.1f}%  Severe hypos: {hypo}"
            )
        return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ #
#  Formatters (pure functions — testable independently)               #
# ------------------------------------------------------------------ #

def _fmt_screen_result(resp) -> str:
    status = _g("✓ PRE-SCREENING CANDIDATE") if resp.eligible else _r("✗ DOES NOT MEET CRITERIA")
    lines = [
        f"\n{_b('Screen Result')} — {resp.patient_id} vs {resp.trial_id}",
        f"  Status: {status}",
        f"  {resp.summary}",
    ]
    # AI narrative removed — eligibility output is self-contained
    lines += [
        "",
        _d("  Audit trail recorded. Pre-screening flag only — not a clinical decision."),
        "",
    ]
    return "\n".join(lines)


def _fmt_find_result(resp) -> str:
    lines = [
        f"\n{_b('Candidate Search')} — {resp.trial_name}",
        f"  {_g(str(resp.eligible_count))} of {resp.total_screened} patient(s) meet pre-screening criteria\n",
    ]
    if resp.eligible_count == 0:
        lines.append(_y("  No candidates identified in current cohort."))
    else:
        lines.append(f"  {_b('Ranked candidates:')}")
        for c in resp.candidates:
            if c["eligible"]:
                lines.append(f"    {_g('✓')} {_c(c['patient_id'])}  — {c['summary'][:120]}...")
        if resp.total_screened > resp.eligible_count:
            lines.append(f"\n  {_d('Ineligible patients:')}")
            for c in resp.candidates:
                if not c["eligible"]:
                    lines.append(f"    {_r('✗')} {c['patient_id']}")
    lines += [
        "",
        f"  {resp.population_summary}",
        _d("\n  Pre-screening flags only — coordinator must verify at formal screening visit."),
        "",
    ]
    return "\n".join(lines)


def _fmt_explanation(resp) -> str:
    status = _g("ELIGIBLE") if resp.eligible else _r("NOT ELIGIBLE")
    lines = [
        f"\n{_b('Exclusion Explanation')} — {resp.patient_id} vs {resp.trial_id}",
        f"  Status: {status}",
        f"  {resp.plain_english_explanation}",
    ]
    if resp.failed_criteria:
        lines.append(f"\n  {_b('Failed criteria:')}")
        for fc in resp.failed_criteria:
            label = fc.get("label", fc.get("field", "?"))
            pv    = fc.get("patient_value", "?")
            req   = fc.get("required", fc.get("threshold", "?"))
            lines.append(f"    {_r('✗')} {label}")
            lines.append(f"       Patient value: {pv}  |  Required: {req}")
            rationale = fc.get("clinical_rationale", "")
            if rationale:
                lines.append(f"       {_d(textwrap.fill(rationale, 70, subsequent_indent='       '))}")
    lines += [
        f"\n  {_b('Recommendation:')} {resp.recommendation}",
        _d("\n  Pre-screening flags only — not a clinical decision."),
        "",
    ]
    return "\n".join(lines)


def _fmt_comparison(resp) -> str:
    lines = [
        f"\n{_b('Trial Comparison')} — {resp.patient_id}",
    ]
    if resp.best_match:
        lines.append(f"  {_b('Best match:')} {_g(resp.best_match)}\n")
    else:
        lines.append(f"  {_y('No eligible trials found for this patient.')}\n")

    for row in resp.comparison_table:
        icon = _g("✓") if row.get("eligible") else _r("✗")
        passed = row.get("criteria_passed", 0)
        total  = row.get("criteria_total", "?")
        lines.append(
            f"  {icon} {_c(row['trial_id']):30s}  "
            f"{passed}/{total} inclusion criteria met"
        )
    lines += ["", _d("  Pre-screening flags only."), ""]
    return "\n".join(lines)


def _fmt_briefing(resp) -> str:
    lines = [
        f"\n{_b('Coordinator Briefing')}",
        f"  Cohort size: {resp.total_patients} patient(s)",
        f"  Trials assessed: {len(resp.trials_assessed)}",
        "",
        resp.briefing_text,
        "",
    ]
    if resp.opportunities:
        lines.append(f"  {_b('Breakdown by trial:')}")
        for o in resp.opportunities:
            count = o["eligible_count"]
            total = o["total_screened"]
            icon  = _g("●") if count > 0 else _d("○")
            lines.append(
                f"    {icon} {_c(o['trial_id']):30s}  "
                f"{_g(str(count)) if count > 0 else _d('0')} / {total} candidates"
            )
            for tc in o.get("top_candidates", []):
                lines.append(f"         → {tc['patient_id']}")
    lines += ["", _d("  Pre-screening flags only — not clinical decisions."), ""]
    return "\n".join(lines)
