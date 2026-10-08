"""
repl/intent_parser.py — GlucoFlow Natural Language Intent Parser

Maps plain-English coordinator queries to structured MCP action requests.

Design philosophy:
  No LLM involved in parsing — parsing is a deterministic keyword/pattern
  matcher. This is deliberate:
    1. Zero latency — intent resolution is instant (no Bedrock round-trip)
    2. Zero cost — pattern matching is free
    3. Fully testable — deterministic output for every input string
    4. Predictable — coordinators can learn what phrases trigger which actions

  Bedrock AI is used ONLY for the narrative output — the part where nuanced
  clinical language matters. The routing decision is never left to the LLM.

Intent resolution precedence (first match wins):
  1. 'briefing' / 'overview' / 'summary' / 'opportunities'
     → get_coordinator_briefing
  2. 'compare' / 'which trial' / 'best trial' / 'all trials'
     → compare_trials_for_patient (requires patient_id)
  3. 'why' / 'explain' / 'reason' / 'not qualify' / 'ineligible'
     → explain_exclusion (requires patient_id + trial_id)
  4. 'who' / 'find' / 'list' / 'candidates' / 'eligible patients'
     → find_eligible_patients (requires trial_id)
  5. 'screen' / 'qualify' / 'check' / 'does' / patient_id mentioned
     → screen_patient_for_trial (requires patient_id + trial_id)
  6. unrecognised
     → UNKNOWN intent with suggested rephrasing

Trial aliases — coordinators can use short names:
  "diamond" / "diamond trial" / "diamond cgm"  → diamond_t1dm
  "closed loop" / "aps" / "artificial pancreas" → closed_loop_candidate
  "glp1" / "glp-1" / "glp 1" / "t2dm trial"   → glp1_t2dm
"""

import re
from dataclasses import dataclass
from typing import Optional


# ------------------------------------------------------------------ #
#  Intent enum                                                         #
# ------------------------------------------------------------------ #

class Intent:
    SCREEN_PATIENT       = "screen_patient_for_trial"
    FIND_ELIGIBLE        = "find_eligible_patients"
    EXPLAIN_EXCLUSION    = "explain_exclusion"
    COMPARE_TRIALS       = "compare_trials_for_patient"
    COORDINATOR_BRIEFING = "get_coordinator_briefing"
    LIST_TRIALS          = "list_trials"
    LIST_PATIENTS        = "list_patients"
    HELP                 = "help"
    QUIT                 = "quit"
    UNKNOWN              = "unknown"


# ------------------------------------------------------------------ #
#  ParsedIntent                                                        #
# ------------------------------------------------------------------ #

@dataclass
class ParsedIntent:
    """
    The result of parsing a coordinator's natural language query.

    Attributes
    ----------
    intent      : one of the Intent constants
    patient_id  : extracted patient ID, or None
    trial_id    : extracted + normalised trial ID, or None
    raw_query   : the original query string
    confidence  : "high" | "medium" | "low" — how certain the parser is
    suggestion  : if confidence is low, a rephrasing suggestion
    """
    intent:     str
    raw_query:  str
    patient_id: Optional[str] = None
    trial_id:   Optional[str] = None
    confidence: str = "high"
    suggestion: Optional[str] = None


# ------------------------------------------------------------------ #
#  Trial alias map                                                     #
# ------------------------------------------------------------------ #

TRIAL_ALIASES = {
    # DIAMOND
    "diamond":              "diamond_t1dm",
    "diamond trial":        "diamond_t1dm",
    "diamond cgm":          "diamond_t1dm",
    "diamond t1dm":         "diamond_t1dm",
    "diamond_t1dm":         "diamond_t1dm",
    "t1dm":                 "diamond_t1dm",
    "type 1":               "diamond_t1dm",
    "type1":                "diamond_t1dm",

    # Closed-loop
    "closed loop":          "closed_loop_candidate",
    "closed_loop":          "closed_loop_candidate",
    "closed_loop_candidate":"closed_loop_candidate",
    "aps":                  "closed_loop_candidate",
    "artificial pancreas":  "closed_loop_candidate",
    "loop":                 "closed_loop_candidate",
    "pump":                 "closed_loop_candidate",

    # GLP-1
    "glp1":                 "glp1_t2dm",
    "glp-1":                "glp1_t2dm",
    "glp 1":                "glp1_t2dm",
    "glp1_t2dm":            "glp1_t2dm",
    "t2dm":                 "glp1_t2dm",
    "type 2":               "glp1_t2dm",
    "type2":                "glp1_t2dm",
    "sustain":              "glp1_t2dm",
    "semaglutide":          "glp1_t2dm",
}

# Known patient IDs — used to extract patient references from queries
KNOWN_PATIENT_IDS = [
    "adolescent_001", "adolescent#001",
    "adult_001",      "adult#001",
    "child_001",      "child#001",
]

# General archetype regex — matches ALL synthetic patient IDs regardless of
# prefix (bare, sim_, dexcom_dexcom_) and number suffix (001-999).
# Examples matched: child_008, sim_adolescent_010, dexcom_dexcom_adult_003
_ARCHETYPE_PATIENT_RE = re.compile(
    r"(?:dexcom_dexcom_|sim_)?(?:adolescent|adult|child)_\d{3}"
)

# Shanghai patient identifiers are four-digit numeric IDs. Restricting this
# pattern to the documented 1xxx/2xxx cohorts avoids treating years, glucose
# thresholds, or trial phases as patient IDs.
_SHANGHAI_PATIENT_RE = re.compile(r"(?<![\w])([12]\d{3})(?![\w])")

# Normalise #-style IDs to _-style
_PATIENT_NORMALISE = {
    "adolescent#001": "adolescent_001",
    "adult#001":      "adult_001",
    "child#001":      "child_001",
}


# ------------------------------------------------------------------ #
#  Parser                                                              #
# ------------------------------------------------------------------ #

class IntentParser:
    """
    Deterministic keyword-based intent parser for coordinator queries.

    Usage
    -----
    parser = IntentParser()
    result = parser.parse("who qualifies for the diamond trial?")
    # result.intent == Intent.FIND_ELIGIBLE
    # result.trial_id == "diamond_t1dm"
    """

    def parse(self, query: str) -> ParsedIntent:
        """
        Parse a natural language query into a ParsedIntent.

        Parameters
        ----------
        query : str — the coordinator's free-text query

        Returns
        -------
        ParsedIntent
        """
        q = query.strip().lower()

        # --- Special commands ---
        if q in ("quit", "exit", "q", "bye", "goodbye"):
            return ParsedIntent(intent=Intent.QUIT, raw_query=query)

        if q in ("help", "?", "h", "commands"):
            return ParsedIntent(intent=Intent.HELP, raw_query=query)

        if any(p in q for p in ("list trial", "show trial", "available trial", "what trial")):
            return ParsedIntent(intent=Intent.LIST_TRIALS, raw_query=query)

        if any(p in q for p in ("list patient", "show patient", "all patient", "who are the patient")):
            return ParsedIntent(intent=Intent.LIST_PATIENTS, raw_query=query)

        # --- Extract patient and trial from query ---
        patient_id = self._extract_patient(q)
        trial_id   = self._extract_trial(q)

        # --- Intent resolution (precedence order) ---

        # 1. Compare trials for a patient. Check this before briefing so
        # "compare all trials for 1006" is not captured by the broad
        # "all trial" briefing phrase.
        if any(p in q for p in (
            "compare", "which trial", "best trial", "best match",
            "all trial", "suitable", "which study"
        )):
            if patient_id:
                return ParsedIntent(
                    intent=Intent.COMPARE_TRIALS,
                    raw_query=query,
                    patient_id=patient_id,
                    confidence="high",
                )
            return ParsedIntent(
                intent=Intent.COMPARE_TRIALS,
                raw_query=query,
                confidence="low",
                suggestion=(
                    "Try: 'compare trials for adult_001' or "
                    "'which trials suit child_001?'"
                ),
            )

        # 2. Coordinator briefing
        if any(p in q for p in (
            "briefing", "overview", "population", "opportunity",
            "opportunities", "across all", "all patients",
            "how many", "recruitment"
        )):
            return ParsedIntent(
                intent=Intent.COORDINATOR_BRIEFING,
                raw_query=query,
                confidence="high",
            )

        # 3. Explain exclusion
        if any(p in q for p in (
            "why", "explain", "reason", "not qualify", "not eligible",
            "ineligible", "excluded", "doesn't qualify", "does not qualify",
            "why not", "what's wrong", "whats wrong"
        )):
            if patient_id and trial_id:
                return ParsedIntent(
                    intent=Intent.EXPLAIN_EXCLUSION,
                    raw_query=query,
                    patient_id=patient_id,
                    trial_id=trial_id,
                    confidence="high",
                )
            confidence = "medium" if (patient_id or trial_id) else "low"
            return ParsedIntent(
                intent=Intent.EXPLAIN_EXCLUSION,
                raw_query=query,
                patient_id=patient_id,
                trial_id=trial_id,
                confidence=confidence,
                suggestion=(
                    "Try: 'why doesn't child_001 qualify for the diamond trial?' "
                    "— I need a patient ID and a trial name."
                ),
            )

        # 4. Find eligible patients (cohort query)
        # Skip if a specific patient_id was extracted — that's a SCREEN_PATIENT query
        if not patient_id and any(p in q for p in (
            "who", "find", "list candidate", "candidates",
            "eligible patient", "qualify for", "qualifies for",
            "who qualify", "who qualifies", "show candidate"
        )):
            if trial_id:
                return ParsedIntent(
                    intent=Intent.FIND_ELIGIBLE,
                    raw_query=query,
                    trial_id=trial_id,
                    confidence="high",
                )
            return ParsedIntent(
                intent=Intent.FIND_ELIGIBLE,
                raw_query=query,
                confidence="low",
                suggestion=(
                    "Try: 'who qualifies for the diamond trial?' or "
                    "'find candidates for the closed-loop study'"
                ),
            )

        # 5. Screen specific patient
        if any(p in q for p in (
            "screen", "check", "does", "is", "qualify", "qualifies",
            "eligible", "can", "assess", "evaluate"
        )) or patient_id:
            if patient_id and trial_id:
                return ParsedIntent(
                    intent=Intent.SCREEN_PATIENT,
                    raw_query=query,
                    patient_id=patient_id,
                    trial_id=trial_id,
                    confidence="high",
                )
            confidence = "medium" if (patient_id or trial_id) else "low"
            return ParsedIntent(
                intent=Intent.SCREEN_PATIENT,
                raw_query=query,
                patient_id=patient_id,
                trial_id=trial_id,
                confidence=confidence,
                suggestion=(
                    "Try: 'screen adult_001 for the diamond trial' or "
                    "'does child_001 qualify for the glp1 trial?'"
                ),
            )

        # 6. Unknown
        return ParsedIntent(
            intent=Intent.UNKNOWN,
            raw_query=query,
            confidence="low",
            suggestion=(
                "I didn't understand that query. Type 'help' to see "
                "what I can do."
            ),
        )

    # ---------------------------------------------------------------- #
    #  Extraction helpers                                               #
    # ---------------------------------------------------------------- #

    def _extract_patient(self, q: str) -> Optional[str]:
        """Extract and normalise an archetype or Shanghai numeric patient ID."""
        # Priority 1: archetype regex (handles bare, sim_, dexcom_dexcom_ prefixes
        # with any 3-digit number — e.g. child_008, sim_adolescent_010)
        arch_match = _ARCHETYPE_PATIENT_RE.search(q)
        if arch_match:
            return arch_match.group(0)

        # Priority 2: legacy exact-match list (kept for #-style normalisation)
        for pid in KNOWN_PATIENT_IDS:
            if pid.replace("#", "_") in q or pid in q:
                return _PATIENT_NORMALISE.get(pid, pid.replace("#", "_"))

        # Priority 3: Numeric Shanghai IDs (1xxx/2xxx documented cohorts).
        for match in _SHANGHAI_PATIENT_RE.finditer(q):
            # A number explicitly introduced as a calendar year is not a
            # patient reference (for example, "trial in 2021").
            prefix = q[max(0, match.start() - 3):match.start()]
            if prefix == "in ":
                continue
            return match.group(1)
        return None

    def _extract_trial(self, q: str) -> Optional[str]:
        """
        Extract and normalise a trial ID from a query string.
        Tries longest alias match first to avoid short-string false positives.
        """
        # Sort by length descending so "diamond cgm" matches before "diamond"
        for alias in sorted(TRIAL_ALIASES.keys(), key=len, reverse=True):
            if alias in q:
                return TRIAL_ALIASES[alias]
        return None
