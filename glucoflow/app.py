"""
app.py — GlucoFlow Web Dashboard (Demo Mode)

Clinical trial coordinator interface showing patient CGM metrics and
trial eligibility. Runs entirely without AWS — synthetic cohort only.

Usage:
    cd /external/gcmMonitor/glucoflow
    pip install flask
    python app.py               # http://localhost:5050
    FLASK_ENV=development python app.py   # with hot-reload

Routes:
    GET /                           — cohort dashboard (3 patient cards)
    GET /trials                     — all 5 trial protocol cards
    GET /screen/<patient_id>/<trial_id> — full eligibility result
    GET /cohort/<trial_id>          — all patients ranked for one trial
"""

import operator as op
from flask import Flask, render_template_string, abort, url_for

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  DEMO DATA — synthetic cohort (same values as CLI REPL)
# ─────────────────────────────────────────────────────────────────────────────

DEMO_COHORT = [
    {
        "patient_id":        "adolescent_001",
        "day":               "2026-10-07",
        "tir_percent":       58.3,
        "mean_glucose":      171.4,
        "hypo_events":       2,
        "severe_hypo_events": 0,
        "cv_percent":        33.1,
        "gmi":               7.9,
        "source_system":     "dexcom_messy",
        "age_group":         "Adolescent",
        "diabetes_type":     "T1DM",
    },
    {
        "patient_id":        "adult_001",
        "day":               "2026-10-07",
        "tir_percent":       64.2,
        "mean_glucose":      162.7,
        "hypo_events":       1,
        "severe_hypo_events": 0,
        "cv_percent":        29.4,
        "gmi":               7.6,
        "source_system":     "dexcom_messy",
        "age_group":         "Adult",
        "diabetes_type":     "T2DM",
    },
    {
        "patient_id":        "child_001",
        "day":               "2026-10-07",
        "tir_percent":       47.1,
        "mean_glucose":      198.3,
        "hypo_events":       6,
        "severe_hypo_events": 2,
        "cv_percent":        44.7,
        "gmi":               8.5,
        "source_system":     "dexcom_messy",
        "age_group":         "Paediatric",
        "diabetes_type":     "T1DM",
    },
]

# ─────────────────────────────────────────────────────────────────────────────
#  TRIAL PROTOCOLS — 3 existing + 2 new v2 protocols (all hardcoded for demo)
# ─────────────────────────────────────────────────────────────────────────────

DEMO_TRIALS = [
    # ── v1 existing ──────────────────────────────────────────────────────────
    {
        "trial_id":    "diamond_t1dm",
        "trial_name":  "DIAMOND CGM Trial — T1DM Arm",
        "phase":       "III",
        "sponsor":     "Dexcom Inc. / Kaiser Permanente",
        "description": (
            "Evaluates whether CGM reduces HbA1c in adults with T1DM on "
            "multiple daily injections. Targets patients with suboptimal "
            "glycaemic control not yet using CGM. Ref: Beck et al., JAMA 2017."
        ),
        "notes": "Based on published DIAMOND trial inclusion/exclusion criteria.",
        "inclusion_criteria": [
            {
                "field": "gmi", "operator": ">=", "threshold": 7.5,
                "human_label": "Estimated HbA1c (GMI) ≥ 7.5%",
                "human_rationale": "DIAMOND enrolled patients with suboptimal control — GMI below 7.5% suggests the patient already has good control.",
            },
            {
                "field": "gmi", "operator": "<=", "threshold": 10.0,
                "human_label": "Estimated HbA1c (GMI) ≤ 10.0%",
                "human_rationale": "Patients with GMI above 10% have severely uncontrolled diabetes requiring more intensive intervention before trial enrolment.",
            },
            {
                "field": "tir_percent", "operator": "<=", "threshold": 70.0,
                "human_label": "Time-in-Range ≤ 70%",
                "human_rationale": "Patients already achieving TIR > 70% are performing well — the trial targets patients who need glycaemic improvement.",
            },
        ],
        "exclusion_criteria": [
            {
                "field": "severe_hypo_events", "operator": ">", "threshold": 1.0,
                "human_label": "More than 1 severe hypoglycaemic event",
                "human_rationale": "Recurrent severe hypoglycaemia requires clinical management before trial enrolment — excluded due to safety considerations.",
            },
            {
                "field": "cv_percent", "operator": ">", "threshold": 54.0,
                "human_label": "Glycaemic variability (CV%) > 54%",
                "human_rationale": "Extreme glucose variability may indicate acute illness or sensor error rather than baseline patient state.",
            },
        ],
    },
    {
        "trial_id":    "closed_loop_candidate",
        "trial_name":  "Closed-Loop APS Candidate Pre-Screening",
        "phase":       "II",
        "sponsor":     "Cambridge APS Trial / JDRF INSPIRED Study",
        "description": (
            "Screens candidates for artificial pancreas system (APS) trials. "
            "Targets patients with high glycaemic variability and frequent "
            "hypoglycaemia not achieving TIR targets. Ref: Hovorka et al., NEJM 2014."
        ),
        "notes": "High CV% and frequent hypos are primary CGM-based signals for APS candidacy.",
        "inclusion_criteria": [
            {
                "field": "cv_percent", "operator": ">=", "threshold": 36.0,
                "human_label": "Glycaemic variability (CV%) ≥ 36%",
                "human_rationale": "CV ≥ 36% is the ADA threshold for high variability. APS trials target patients where closed-loop adjustments provide greatest benefit.",
            },
            {
                "field": "hypo_events", "operator": ">=", "threshold": 3.0,
                "human_label": "3 or more hypoglycaemic events per day",
                "human_rationale": "Frequent hypoglycaemia indicates the current insulin regimen is over-aggressive — exactly the problem closed-loop algorithms are designed to solve.",
            },
            {
                "field": "tir_percent", "operator": "<=", "threshold": 65.0,
                "human_label": "Time-in-Range ≤ 65%",
                "human_rationale": "APS trials enrol patients not meeting the 70% TIR target. Patients above 65% TIR may not show significant improvement.",
            },
        ],
        "exclusion_criteria": [
            {
                "field": "mean_glucose", "operator": ">", "threshold": 300.0,
                "human_label": "Mean glucose > 300 mg/dL",
                "human_rationale": "Mean glucose above 300 mg/dL suggests hyperglycaemic crisis requiring immediate clinical intervention before device trial.",
            },
            {
                "field": "gmi", "operator": ">", "threshold": 10.5,
                "human_label": "GMI (estimated HbA1c) > 10.5%",
                "human_rationale": "GMI above 10.5% indicates severely uncontrolled diabetes. APS trials require patients to be in a stable metabolic state.",
            },
        ],
    },
    {
        "trial_id":    "glp1_t2dm",
        "trial_name":  "GLP-1 Receptor Agonist T2DM Efficacy Trial",
        "phase":       "III",
        "sponsor":     "Novo Nordisk (SUSTAIN-6 / PIONEER programme)",
        "description": (
            "Screens T2DM patients who are candidates for GLP-1 RA trials "
            "(semaglutide/dulaglutide). Targets patients with suboptimal control "
            "on oral agents or basal insulin. Ref: Marso et al., NEJM 2016."
        ),
        "notes": "v2 will add: T2DM confirmation, eGFR ≥ 30, cardiovascular disease history.",
        "inclusion_criteria": [
            {
                "field": "gmi", "operator": ">=", "threshold": 7.5,
                "human_label": "Estimated HbA1c (GMI) ≥ 7.5%",
                "human_rationale": "SUSTAIN-6 enrolled patients with HbA1c ≥ 7.5% at baseline, indicating suboptimal control on current therapy.",
            },
            {
                "field": "gmi", "operator": "<=", "threshold": 10.5,
                "human_label": "Estimated HbA1c (GMI) ≤ 10.5%",
                "human_rationale": "Patients with HbA1c > 10.5% typically require more aggressive therapy than a single GLP-1 agent can provide safely.",
            },
            {
                "field": "tir_percent", "operator": "<=", "threshold": 70.0,
                "human_label": "Time-in-Range ≤ 70%",
                "human_rationale": "Patients not achieving the 70% TIR target are the target population for GLP-1 RA intensification.",
            },
            {
                "field": "mean_glucose", "operator": ">=", "threshold": 154.0,
                "human_label": "Mean glucose ≥ 154 mg/dL",
                "human_rationale": "Mean glucose ≥ 154 mg/dL confirms the patient's hyperglycaemic burden justifies pharmacological intensification.",
            },
        ],
        "exclusion_criteria": [
            {
                "field": "severe_hypo_events", "operator": ">", "threshold": 0.0,
                "human_label": "Any severe hypoglycaemic event",
                "human_rationale": "Patients with severe hypoglycaemia may be on insulin with hypoglycaemia risk — clinical assessment required before adding GLP-1 agent.",
            },
            {
                "field": "mean_glucose", "operator": ">", "threshold": 270.0,
                "human_label": "Mean glucose > 270 mg/dL",
                "human_rationale": "Mean glucose above 270 mg/dL likely requires insulin initiation rather than GLP-1 RA alone — refer for medication review.",
            },
        ],
    },
    # ── v2 new protocols ─────────────────────────────────────────────────────
    {
        "trial_id":    "rescue_aid_t1dm",
        "trial_name":  "RESCUE-AID: Automated Insulin Delivery in High-Risk T1DM",
        "phase":       "II",
        "sponsor":     "JDRF / Helmsley Charitable Trust",
        "description": (
            "Pre-screens T1DM patients with severe glycaemic instability — "
            "frequent severe hypos AND high variability — for enrolment into "
            "an automated insulin delivery (AID) rescue study. Targets the "
            "highest-risk subgroup where hybrid closed-loop therapy shows the "
            "greatest safety and efficacy benefit. Ref: Tauschmann et al., Lancet 2018."
        ),
        "notes": (
            "v2 protocol. Adds severe_hypo_events as a primary inclusion signal "
            "distinguishing this trial from standard APS candidacy screening."
        ),
        "inclusion_criteria": [
            {
                "field": "cv_percent", "operator": ">=", "threshold": 40.0,
                "human_label": "Glycaemic variability (CV%) ≥ 40%",
                "human_rationale": "CV ≥ 40% is the hallmark of high-risk T1DM glucose instability. The AID system's micro-dosing algorithm provides maximum benefit above this threshold.",
            },
            {
                "field": "severe_hypo_events", "operator": ">=", "threshold": 1.0,
                "human_label": "At least 1 severe hypoglycaemic event (< 54 mg/dL)",
                "human_rationale": "Documented severe hypoglycaemia confirms the patient's current regimen carries life-threatening risk — the primary safety endpoint the AID system is designed to eliminate.",
            },
            {
                "field": "gmi", "operator": ">=", "threshold": 8.0,
                "human_label": "Estimated HbA1c (GMI) ≥ 8.0%",
                "human_rationale": "GMI ≥ 8.0% confirms suboptimal overall control co-existing with the severe hypo risk — ruling out patients who are tightly controlled with isolated hypo episodes.",
            },
        ],
        "exclusion_criteria": [
            {
                "field": "mean_glucose", "operator": ">", "threshold": 320.0,
                "human_label": "Mean glucose > 320 mg/dL",
                "human_rationale": "Mean glucose above 320 mg/dL indicates acute hyperglycaemic crisis requiring immediate stabilisation before AID device enrolment.",
            },
            {
                "field": "gmi", "operator": ">", "threshold": 11.0,
                "human_label": "GMI (estimated HbA1c) > 11.0%",
                "human_rationale": "Extremely uncontrolled patients (GMI > 11%) require comprehensive diabetes management restructuring before device-based therapy is safe.",
            },
        ],
    },
    {
        "trial_id":    "clarity_t2dm",
        "trial_name":  "CLARITY: CGM-Guided Lifestyle Intervention in Early T2DM",
        "phase":       "II",
        "sponsor":     "AstraZeneca / NHS Diabetes Prevention Programme",
        "description": (
            "Evaluates CGM-guided dietary and physical activity coaching as a "
            "first-line intervention in early T2DM patients with mild-to-moderate "
            "hyperglycaemia. The trial targets patients not yet on injectable therapy "
            "with low hypoglycaemia risk — the ideal profile for behavioural "
            "intervention before pharmacological escalation. "
            "Ref: Lean et al., Lancet 2018 (DiRECT trial design influence)."
        ),
        "notes": (
            "v2 protocol. Uniquely targets patients with LOW hypo burden (≤ 3/day) "
            "and NO severe hypos — distinguishing the lifestyle-candidate phenotype "
            "from high-risk T1DM patients who dominate other trial pools."
        ),
        "inclusion_criteria": [
            {
                "field": "tir_percent", "operator": "<=", "threshold": 70.0,
                "human_label": "Time-in-Range ≤ 70%",
                "human_rationale": "Patients not at TIR target are the intervention population — those already at 70%+ have achieved lifestyle control and are not candidates.",
            },
            {
                "field": "mean_glucose", "operator": ">=", "threshold": 140.0,
                "human_label": "Mean glucose ≥ 140 mg/dL",
                "human_rationale": "Mean glucose ≥ 140 mg/dL (~HbA1c ≥ 6.5%) confirms persistent hyperglycaemia justifying structured CGM-guided coaching intervention.",
            },
            {
                "field": "hypo_events", "operator": "<=", "threshold": 3.0,
                "human_label": "Hypoglycaemic events ≤ 3 per day",
                "human_rationale": "Low hypo frequency confirms the patient is not on insulin or sulfonylureas with aggressive dosing — a prerequisite for safe lifestyle-only intervention.",
            },
        ],
        "exclusion_criteria": [
            {
                "field": "severe_hypo_events", "operator": ">", "threshold": 0.0,
                "human_label": "Any severe hypoglycaemic event",
                "human_rationale": "Any severe hypoglycaemia indicates the patient is on high-risk insulin therapy — requires medication review before lifestyle-only intervention.",
            },
            {
                "field": "gmi", "operator": ">", "threshold": 10.0,
                "human_label": "GMI (estimated HbA1c) > 10.0%",
                "human_rationale": "GMI above 10% indicates T2DM beyond the window where lifestyle intervention alone is sufficient — pharmacological intensification should be prioritised.",
            },
        ],
    },
]

# Index for fast lookup
_TRIAL_INDEX = {t["trial_id"]: t for t in DEMO_TRIALS}
_PATIENT_INDEX = {p["patient_id"]: p for p in DEMO_COHORT}

# ─────────────────────────────────────────────────────────────────────────────
#  ELIGIBILITY ENGINE (inline, zero AWS dependency)
# ─────────────────────────────────────────────────────────────────────────────

_OPS = {
    ">=": op.ge, "<=": op.le, ">": op.gt,
    "<":  op.lt, "==": op.eq, "!=": op.ne,
}


def _check(value, operator, threshold):
    """Apply a comparison operator between patient value and threshold."""
    return _OPS[operator](float(value), float(threshold))


def screen_patient(patient_id, metrics, trial):
    """
    Evaluate a patient's Gold metrics against a trial protocol dict.
    Returns a rich result dict for template rendering.
    """
    inc_results = []
    for c in trial["inclusion_criteria"]:
        raw = metrics.get(c["field"])
        if raw is None:
            passed = False
            patient_value = None
        else:
            patient_value = float(raw)
            passed = _check(patient_value, c["operator"], c["threshold"])
        inc_results.append({
            "field":         c["field"],
            "operator":      c["operator"],
            "threshold":     c["threshold"],
            "human_label":   c["human_label"],
            "human_rationale": c["human_rationale"],
            "patient_value": patient_value,
            "passed":        passed,
            "kind":          "inclusion",
        })

    exc_results = []
    exc_triggered = []
    for c in trial["exclusion_criteria"]:
        raw = metrics.get(c["field"])
        if raw is None:
            triggered = False
            patient_value = None
        else:
            patient_value = float(raw)
            # For exclusion: _check returning True means patient TRIGGERS the rule
            triggered = _check(patient_value, c["operator"], c["threshold"])
        r = {
            "field":         c["field"],
            "operator":      c["operator"],
            "threshold":     c["threshold"],
            "human_label":   c["human_label"],
            "human_rationale": c["human_rationale"],
            "patient_value": patient_value,
            "triggered":     triggered,
            "passed":        not triggered,   # for uniform template rendering
            "kind":          "exclusion",
        }
        exc_results.append(r)
        if triggered:
            exc_triggered.append(r)

    all_inc_passed = all(r["passed"] for r in inc_results)
    eligible = all_inc_passed and len(exc_triggered) == 0

    # ── AI narrative (mocked — no Bedrock call) ───────────────────────────
    narrative = _generate_mock_narrative(
        patient_id, metrics, trial, eligible, inc_results, exc_triggered
    )

    # ── Summary sentence ──────────────────────────────────────────────────
    if eligible:
        summary = (
            f"{patient_id} meets all pre-screening criteria for "
            f"{trial['trial_name']}. "
            f"All {len(inc_results)} inclusion criteria satisfied; "
            f"no exclusion criteria triggered."
        )
    else:
        parts = []
        failed_inc = [r for r in inc_results if not r["passed"]]
        if failed_inc:
            labels = ", ".join(r["human_label"] for r in failed_inc)
            parts.append(f"{len(failed_inc)} inclusion criteria not met: {labels}")
        if exc_triggered:
            labels = ", ".join(r["human_label"] for r in exc_triggered)
            parts.append(f"{len(exc_triggered)} exclusion criteria triggered: {labels}")
        summary = (
            f"{patient_id} does NOT meet pre-screening criteria for "
            f"{trial['trial_name']}. " + " | ".join(parts)
        )

    return {
        "patient_id":       patient_id,
        "trial_id":         trial["trial_id"],
        "trial_name":       trial["trial_name"],
        "eligible":         eligible,
        "inc_results":      inc_results,
        "exc_results":      exc_results,
        "exc_triggered":    exc_triggered,
        "summary":          summary,
        "narrative":        narrative,
        "metrics":          metrics,
    }


def _generate_mock_narrative(patient_id, metrics, trial, eligible, inc_results, exc_triggered):
    """
    Generate a clinical AI narrative without Bedrock. Produces realistic,
    data-driven plain-English text from the actual eligibility computation.
    This mirrors what Claude-3 Sonnet would return in live mode.
    """
    tir  = metrics.get("tir_percent", 0)
    gmi  = metrics.get("gmi", 0)
    mean = metrics.get("mean_glucose", 0)
    hypo = metrics.get("hypo_events", 0)
    shypo = metrics.get("severe_hypo_events", 0)
    cv   = metrics.get("cv_percent", 0)
    name = trial["trial_name"]

    if eligible:
        # Build positive narrative from passing criteria
        strengths = []
        for r in inc_results:
            if r["passed"]:
                if r["field"] == "tir_percent":
                    strengths.append(f"TIR of {tir:.1f}% confirms suboptimal glycaemic control")
                elif r["field"] == "gmi":
                    strengths.append(f"GMI of {gmi:.1f}% aligns with the study's HbA1c target range")
                elif r["field"] == "mean_glucose":
                    strengths.append(f"mean glucose of {mean:.1f} mg/dL supports inclusion")
                elif r["field"] == "cv_percent":
                    strengths.append(f"glycaemic variability (CV {cv:.1f}%) meets the study threshold")
                elif r["field"] in ("hypo_events", "severe_hypo_events"):
                    strengths.append(f"hypoglycaemia profile meets protocol safety parameters")
        strength_str = "; ".join(strengths[:3]) if strengths else "all CGM metrics within protocol bounds"

        return (
            f"**Pre-screening recommendation: CANDIDATE** ✓\n\n"
            f"Based on {patient_id}'s CGM metrics from 2026-10-07, this patient "
            f"presents a strong profile for {name}. The {strength_str}. "
            f"With no exclusion criteria triggered, the patient is cleared for "
            f"formal coordinator review and invitation to the screening visit.\n\n"
            f"Key metrics: TIR {tir:.1f}% | GMI {gmi:.1f}% | Mean glucose {mean:.1f} mg/dL | "
            f"CV {cv:.1f}% | Hypo events {hypo}/day | Severe hypos {shypo}/day.\n\n"
            f"*Note: This is an automated pre-screening flag. A qualified "
            f"coordinator or clinician must review the patient's full medical "
            f"history before extending a trial invitation.*"
        )
    else:
        # Build explanatory narrative from failures
        parts = []
        failed_inc = [r for r in inc_results if not r["passed"]]

        if failed_inc:
            for r in failed_inc[:2]:
                field_name = r["field"].replace("_", " ")
                pv = r["patient_value"]
                pv_str = f"{pv:.1f}" if pv is not None else "unavailable"
                parts.append(
                    f"{field_name} ({pv_str}) does not satisfy the requirement "
                    f"{r['operator']} {r['threshold']} ({r['human_label']})"
                )

        if exc_triggered:
            for r in exc_triggered[:2]:
                field_name = r["field"].replace("_", " ")
                pv = r["patient_value"]
                pv_str = f"{pv:.1f}" if pv is not None else "unavailable"
                parts.append(
                    f"{field_name} ({pv_str}) triggers the exclusion criterion "
                    f"{r['operator']} {r['threshold']}: {r['human_label']}"
                )

        reasons_str = "; ".join(parts) if parts else "protocol criteria not met"

        return (
            f"**Pre-screening recommendation: NOT A CANDIDATE** ✗\n\n"
            f"{patient_id} does not meet the pre-screening criteria for {name}. "
            f"Specifically: {reasons_str}. "
            f"The patient's CGM data (TIR {tir:.1f}%, GMI {gmi:.1f}%, mean glucose "
            f"{mean:.1f} mg/dL, CV {cv:.1f}%) places them outside the protocol's "
            f"target population for this study.\n\n"
            f"*Coordinator action: Review whether any other open trials are suitable "
            f"(see /trials for the full protocol list), or flag for standard care "
            f"pathway. Pre-screening flags are not clinical decisions.*"
        )


def tir_color(tir):
    """Return CSS colour class based on TIR percentage."""
    if tir >= 70:
        return "green"
    elif tir >= 50:
        return "amber"
    return "red"


def tir_label(tir):
    if tir >= 70:
        return "On Target"
    elif tir >= 50:
        return "Suboptimal"
    return "Poor Control"


# ─────────────────────────────────────────────────────────────────────────────
#  SHARED CSS / DESIGN SYSTEM (dark theme, matching kanban.html palette)
# ─────────────────────────────────────────────────────────────────────────────

BASE_CSS = """
:root {
  --bg:        #0f172a;
  --surface:   #1e2235;
  --surface2:  #252b3d;
  --border:    #2e3650;
  --accent:    #6c63ff;
  --accent2:   #00d9c0;
  --text:      #e2e8f0;
  --subtext:   #8892b0;
  --green:     #34d399;
  --green-bg:  #0a2e1f;
  --amber:     #fbbf24;
  --amber-bg:  #2a1e07;
  --red:       #f87171;
  --red-bg:    #2a0f0f;
  --blue:      #60a5fa;
  --blue-bg:   #0f1e38;
  --purple:    #a78bfa;
  --purple-bg: #1e1535;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
}
a { color: var(--accent2); text-decoration: none; }
a:hover { text-decoration: underline; }

/* ── Nav ── */
nav {
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 0 32px;
  display: flex;
  align-items: center;
  gap: 0;
  height: 56px;
}
.nav-logo {
  display: flex; align-items: center; gap: 10px;
  font-weight: 700; font-size: 17px;
  color: var(--text);
  margin-right: 32px;
}
.nav-logo .logo-icon {
  width: 32px; height: 32px; border-radius: 8px;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  display: flex; align-items: center; justify-content: center;
  font-size: 16px;
}
.nav-link {
  padding: 0 16px;
  height: 56px;
  display: flex; align-items: center;
  font-size: 14px;
  color: var(--subtext);
  border-bottom: 2px solid transparent;
  transition: color .15s, border-color .15s;
}
.nav-link:hover { color: var(--text); text-decoration: none; }
.nav-link.active { color: var(--accent2); border-bottom-color: var(--accent2); }
.nav-badge {
  margin-left: 6px;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 1px 7px;
  font-size: 11px;
  color: var(--subtext);
}
.demo-pill {
  margin-left: auto;
  background: rgba(251,191,36,.12);
  border: 1px solid rgba(251,191,36,.3);
  color: var(--amber);
  border-radius: 20px;
  padding: 3px 12px;
  font-size: 12px;
  font-weight: 600;
}

/* ── Page layout ── */
.page {
  max-width: 1200px;
  margin: 0 auto;
  padding: 32px 24px;
}
.page-header {
  margin-bottom: 28px;
}
.page-header h1 {
  font-size: 24px; font-weight: 700;
  letter-spacing: -0.5px;
}
.page-header p {
  margin-top: 6px;
  font-size: 14px;
  color: var(--subtext);
}
.breadcrumb {
  display: flex; align-items: center; gap: 8px;
  font-size: 13px; color: var(--subtext);
  margin-bottom: 16px;
}
.breadcrumb span { color: var(--subtext); }

/* ── Cards ── */
.cards-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 20px;
}
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  overflow: hidden;
  transition: border-color .15s, transform .1s;
}
.card:hover { border-color: var(--accent); transform: translateY(-1px); }
.card-header {
  padding: 16px 20px;
  display: flex; align-items: flex-start; gap: 12px;
  border-bottom: 1px solid var(--border);
}
.card-icon {
  width: 40px; height: 40px; border-radius: 10px;
  display: flex; align-items: center; justify-content: center;
  font-size: 18px; flex-shrink: 0;
}
.card-icon.green  { background: var(--green-bg);  color: var(--green); }
.card-icon.amber  { background: var(--amber-bg);  color: var(--amber); }
.card-icon.red    { background: var(--red-bg);    color: var(--red); }
.card-icon.blue   { background: var(--blue-bg);   color: var(--blue); }
.card-icon.purple { background: var(--purple-bg); color: var(--purple); }
.card-title-block { flex: 1; min-width: 0; }
.card-title {
  font-size: 16px; font-weight: 600;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.card-subtitle {
  font-size: 12px; color: var(--subtext); margin-top: 2px;
}
.card-body { padding: 16px 20px; }
.card-footer {
  padding: 12px 20px;
  border-top: 1px solid var(--border);
  display: flex; align-items: center; gap: 10px;
}

/* ── Metrics grid ── */
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
  margin-bottom: 14px;
}
.metric {
  background: var(--surface2);
  border-radius: 8px;
  padding: 10px 14px;
}
.metric-label {
  font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.5px; color: var(--subtext);
  margin-bottom: 4px;
}
.metric-value {
  font-size: 20px; font-weight: 700;
  letter-spacing: -0.5px;
}
.metric-value.green  { color: var(--green); }
.metric-value.amber  { color: var(--amber); }
.metric-value.red    { color: var(--red); }
.metric-sub {
  font-size: 11px; color: var(--subtext); margin-top: 2px;
}

/* ── Status pills ── */
.pill {
  display: inline-flex; align-items: center; gap: 5px;
  border-radius: 20px; padding: 3px 10px;
  font-size: 12px; font-weight: 600;
}
.pill-green  { background: rgba(52,211,153,.15); color: var(--green); border: 1px solid rgba(52,211,153,.3); }
.pill-amber  { background: rgba(251,191,36,.12); color: var(--amber); border: 1px solid rgba(251,191,36,.3); }
.pill-red    { background: rgba(248,113,113,.12); color: var(--red);   border: 1px solid rgba(248,113,113,.3); }
.pill-blue   { background: rgba(96,165,250,.12);  color: var(--blue);  border: 1px solid rgba(96,165,250,.3); }
.pill-purple { background: rgba(167,139,250,.12); color: var(--purple);border: 1px solid rgba(167,139,250,.3); }
.pill-ghost  { background: transparent; color: var(--subtext); border: 1px solid var(--border); }

/* ── Table ── */
.table-wrap {
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: 10px;
}
table {
  width: 100%; border-collapse: collapse;
  font-size: 13px;
}
th {
  background: var(--surface2);
  color: var(--subtext);
  font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.5px;
  padding: 10px 14px; text-align: left;
  border-bottom: 1px solid var(--border);
}
td {
  padding: 10px 14px;
  border-bottom: 1px solid rgba(46,54,80,.5);
  vertical-align: top;
}
tr:last-child td { border-bottom: none; }
tr:hover td { background: var(--surface2); }

/* ── Icon checks ── */
.check   { color: var(--green); font-weight: 700; }
.cross   { color: var(--red);   font-weight: 700; }
.trigger { color: var(--amber); font-weight: 700; }

/* ── Eligibility result ── */
.result-banner {
  border-radius: 12px;
  padding: 20px 24px;
  display: flex; align-items: center; gap: 16px;
  margin-bottom: 28px;
  border: 1px solid;
}
.result-banner.eligible {
  background: var(--green-bg);
  border-color: rgba(52,211,153,.3);
}
.result-banner.ineligible {
  background: var(--red-bg);
  border-color: rgba(248,113,113,.3);
}
.result-icon {
  width: 52px; height: 52px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 26px; flex-shrink: 0;
}
.result-banner.eligible .result-icon { background: rgba(52,211,153,.15); }
.result-banner.ineligible .result-icon { background: rgba(248,113,113,.15); }
.result-title { font-size: 18px; font-weight: 700; }
.result-subtitle { font-size: 13px; color: var(--subtext); margin-top: 4px; }

/* ── Narrative box ── */
.narrative {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 18px 22px;
  font-size: 14px; line-height: 1.7;
  white-space: pre-wrap;
  margin-bottom: 24px;
}
.narrative strong, .narrative b { color: var(--accent2); }

/* ── Section header ── */
.section-header {
  display: flex; align-items: center; gap: 10px;
  margin-bottom: 14px; margin-top: 28px;
}
.section-header h2 {
  font-size: 15px; font-weight: 600;
}
.section-header .section-count {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 1px 8px;
  font-size: 12px; color: var(--subtext);
}

/* ── Trial cards ── */
.trial-phase {
  font-size: 11px; font-weight: 700;
  letter-spacing: 0.8px;
}
.trial-desc {
  font-size: 13px; color: var(--subtext);
  line-height: 1.5;
  margin-top: 8px;
}

/* ── Cohort rank table ── */
.rank-badge {
  display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 50%;
  font-size: 12px; font-weight: 700;
  color: var(--subtext);
}
.rank-badge.top { background: rgba(52,211,153,.12); border-color: rgba(52,211,153,.3); color: var(--green); }

/* ── Patient metrics mini bar ── */
.mini-metrics {
  display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px;
}
.mini-metric {
  font-size: 11px;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 2px 8px;
  color: var(--subtext);
}
.mini-metric span { color: var(--text); font-weight: 600; }

/* ── Disclaimer banner ── */
.disclaimer {
  margin-top: 40px;
  padding: 12px 18px;
  background: rgba(46,54,80,.3);
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 12px; color: var(--subtext);
  display: flex; gap: 8px; align-items: flex-start;
}

/* ── Button ── */
.btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 7px 16px;
  border-radius: 8px;
  font-size: 13px; font-weight: 600;
  cursor: pointer; border: none;
  transition: opacity .15s;
  text-decoration: none;
}
.btn:hover { opacity: .85; text-decoration: none; }
.btn-primary { background: var(--accent); color: #fff; }
.btn-ghost   { background: var(--surface2); color: var(--text); border: 1px solid var(--border); }

/* ── Responsive ── */
@media (max-width: 768px) {
  .cards-grid { grid-template-columns: 1fr; }
  nav { padding: 0 16px; }
  .page { padding: 20px 16px; }
}
"""

# ─────────────────────────────────────────────────────────────────────────────
#  BASE TEMPLATE
# ─────────────────────────────────────────────────────────────────────────────

BASE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{{ title }} — GlucoFlow</title>
<style>{{ css }}</style>
</head>
<body>

<nav>
  <a class="nav-logo" href="/">
    <div class="logo-icon">⚡</div>
    GlucoFlow
  </a>
  <a class="nav-link {{ 'active' if active=='dashboard' }}" href="/">Dashboard</a>
  <a class="nav-link {{ 'active' if active=='trials' }}" href="/trials">
    Trials
    <span class="nav-badge">5</span>
  </a>
  <span class="demo-pill">⚡ Demo Mode</span>
</nav>

<div class="page">
  {{ content }}

  <div class="disclaimer">
    ⚠️&nbsp; <span>All outputs are <strong>pre-screening flags only</strong> and are not clinical decisions.
    A qualified coordinator or clinician must review all candidates before extending a trial invitation.
    No real patient data is used — this is a synthetic demo cohort.</span>
  </div>
</div>
</body>
</html>"""


def render_page(title, active, content):
    """Render a page using the base template."""
    return render_template_string(
        BASE_TEMPLATE,
        title=title,
        active=active,
        content=content,
        css=BASE_CSS,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTE: GET /  — Cohort Dashboard
# ─────────────────────────────────────────────────────────────────────────────

DASHBOARD_TEMPLATE = """
<div class="page-header">
  <h1>🩺 Patient Cohort Dashboard</h1>
  <p>CGM Gold-layer metrics for the active synthetic demo cohort · Data as of 2026-10-07</p>
</div>

<div class="cards-grid">
  {% for p in patients %}
  {% set color = tir_color(p.tir_percent) %}
  {% set label = tir_label(p.tir_percent) %}
  <div class="card">
    <div class="card-header">
      <div class="card-icon {{ color }}">👤</div>
      <div class="card-title-block">
        <div class="card-title">{{ p.patient_id }}</div>
        <div class="card-subtitle">{{ p.age_group }} · {{ p.diabetes_type }} · {{ p.day }}</div>
      </div>
      <div>
        <span class="pill pill-{{ color }}">
          {{ '●' }} {{ label }}
        </span>
      </div>
    </div>
    <div class="card-body">
      <div class="metrics-grid">
        <div class="metric">
          <div class="metric-label">Time in Range</div>
          <div class="metric-value {{ color }}">{{ '%.1f'|format(p.tir_percent) }}%</div>
          <div class="metric-sub">Target: ≥ 70%</div>
        </div>
        <div class="metric">
          <div class="metric-label">GMI (est. HbA1c)</div>
          <div class="metric-value {{ 'green' if p.gmi < 7.5 else ('amber' if p.gmi < 9 else 'red') }}">{{ '%.1f'|format(p.gmi) }}%</div>
          <div class="metric-sub">Target: &lt; 7.0%</div>
        </div>
        <div class="metric">
          <div class="metric-label">Mean Glucose</div>
          <div class="metric-value {{ 'green' if p.mean_glucose < 154 else ('amber' if p.mean_glucose < 180 else 'red') }}">{{ '%.0f'|format(p.mean_glucose) }}</div>
          <div class="metric-sub">mg/dL · Target: &lt; 154</div>
        </div>
        <div class="metric">
          <div class="metric-label">Hypo Events</div>
          <div class="metric-value {{ 'green' if p.hypo_events == 0 else ('amber' if p.hypo_events <= 2 else 'red') }}">{{ p.hypo_events }}</div>
          <div class="metric-sub">{{ p.severe_hypo_events }} severe (&lt; 54 mg/dL)</div>
        </div>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <span class="pill pill-ghost">CV {{ '%.1f'|format(p.cv_percent) }}%</span>
        <span class="pill pill-ghost">{{ p.source_system }}</span>
      </div>
    </div>
    <div class="card-footer">
      {% for trial in trials[:3] %}
      {% set res = screen(p.patient_id, p, trial) %}
      <a class="btn btn-ghost" style="padding:5px 10px;font-size:12px;"
         href="/screen/{{ p.patient_id }}/{{ trial.trial_id }}"
         title="{{ trial.trial_name }}">
        {{ '✓' if res.eligible else '✗' }}
        {{ trial.trial_id.replace('_',' ').title()[:14] }}
      </a>
      {% endfor %}
      <a class="btn btn-primary" style="margin-left:auto;padding:5px 12px;font-size:12px;"
         href="/cohort/diamond_t1dm">All Trials →</a>
    </div>
  </div>
  {% endfor %}
</div>

<div class="section-header" style="margin-top:36px;">
  <h2>Quick Eligibility Matrix</h2>
  <span class="section-count">{{ patients|length }} patients × {{ trials|length }} trials</span>
</div>

<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th>Patient</th>
        <th>TIR%</th>
        <th>GMI</th>
        {% for trial in trials %}
        <th>{{ trial.trial_id.replace('_',' ').upper()[:16] }}</th>
        {% endfor %}
      </tr>
    </thead>
    <tbody>
      {% for p in patients %}
      {% set color = tir_color(p.tir_percent) %}
      <tr>
        <td>
          <strong>{{ p.patient_id }}</strong>
          <div style="font-size:11px;color:var(--subtext)">{{ p.age_group }} · {{ p.diabetes_type }}</div>
        </td>
        <td><span class="pill pill-{{ color }}">{{ '%.1f'|format(p.tir_percent) }}%</span></td>
        <td>{{ '%.1f'|format(p.gmi) }}%</td>
        {% for trial in trials %}
        {% set res = screen(p.patient_id, p, trial) %}
        <td style="text-align:center;">
          <a href="/screen/{{ p.patient_id }}/{{ trial.trial_id }}"
             style="text-decoration:none;font-size:16px;"
             title="{{ 'Eligible' if res.eligible else 'Not Eligible' }}">
            {{ '✅' if res.eligible else '❌' }}
          </a>
        </td>
        {% endfor %}
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
"""


@app.route("/")
def dashboard():
    def screen_fn(patient_id, metrics, trial):
        return screen_patient(patient_id, metrics, trial)

    content = render_template_string(
        DASHBOARD_TEMPLATE,
        patients=DEMO_COHORT,
        trials=DEMO_TRIALS,
        screen=screen_fn,
        tir_color=tir_color,
        tir_label=tir_label,
    )
    return render_page("Dashboard", "dashboard", content)


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTE: GET /trials  — Trial Protocols
# ─────────────────────────────────────────────────────────────────────────────

TRIALS_TEMPLATE = """
<div class="page-header">
  <h1>🧪 Clinical Trial Protocols</h1>
  <p>{{ trials|length }} active protocols (3 existing v1 · 2 new v2) · CGM-based eligibility criteria</p>
</div>

<div class="cards-grid">
  {% for trial in trials %}
  {% set n_criteria = trial.inclusion_criteria|length + trial.exclusion_criteria|length %}
  {% set phase_color = {'I':'purple','II':'blue','III':'green','RWE':'amber'}.get(trial.phase, 'ghost') %}
  <div class="card">
    <div class="card-header">
      <div class="card-icon {{ phase_color }}">🧬</div>
      <div class="card-title-block">
        <div class="card-title">{{ trial.trial_name }}</div>
        <div class="card-subtitle">{{ trial.sponsor }}</div>
      </div>
    </div>
    <div class="card-body">
      <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center;">
        <span class="pill pill-{{ phase_color }}">Phase {{ trial.phase }}</span>
        <span class="pill pill-ghost">{{ n_criteria }} criteria</span>
        <span class="pill pill-ghost">{{ trial.inclusion_criteria|length }} inclusion</span>
        <span class="pill pill-ghost">{{ trial.exclusion_criteria|length }} exclusion</span>
        {% if 'v2' in trial.notes|lower or 'rescue' in trial.trial_id or 'clarity' in trial.trial_id %}
        <span class="pill pill-purple" style="margin-left:auto;">v2 New</span>
        {% endif %}
      </div>
      <p class="trial-desc">{{ trial.description[:220] }}{% if trial.description|length > 220 %}…{% endif %}</p>
      <div style="margin-top:12px;">
        <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--subtext);margin-bottom:6px;">
          Inclusion Criteria
        </div>
        {% for c in trial.inclusion_criteria %}
        <div style="font-size:12px;color:var(--subtext);padding:3px 0;display:flex;gap:6px;">
          <span style="color:var(--green);flex-shrink:0;">+</span>
          {{ c.human_label }}
        </div>
        {% endfor %}
        {% if trial.exclusion_criteria %}
        <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--subtext);margin-bottom:6px;margin-top:10px;">
          Exclusion Criteria
        </div>
        {% for c in trial.exclusion_criteria %}
        <div style="font-size:12px;color:var(--subtext);padding:3px 0;display:flex;gap:6px;">
          <span style="color:var(--red);flex-shrink:0;">−</span>
          {{ c.human_label }}
        </div>
        {% endfor %}
        {% endif %}
      </div>
    </div>
    <div class="card-footer">
      <a class="btn btn-primary" href="/cohort/{{ trial.trial_id }}">View Cohort →</a>
      {% for p in patients %}
      <a class="btn btn-ghost" style="padding:5px 10px;font-size:12px;"
         href="/screen/{{ p.patient_id }}/{{ trial.trial_id }}">
        {{ p.patient_id.replace('_001','') }}
      </a>
      {% endfor %}
    </div>
  </div>
  {% endfor %}
</div>
"""


@app.route("/trials")
def trials_page():
    content = render_template_string(
        TRIALS_TEMPLATE,
        trials=DEMO_TRIALS,
        patients=DEMO_COHORT,
    )
    return render_page("Trials", "trials", content)


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTE: GET /screen/<patient_id>/<trial_id>  — Eligibility Detail
# ─────────────────────────────────────────────────────────────────────────────

SCREEN_TEMPLATE = """
<div class="breadcrumb">
  <a href="/">Dashboard</a>
  <span>/</span>
  <a href="/cohort/{{ result.trial_id }}">{{ result.trial_id.replace('_',' ').title() }}</a>
  <span>/</span>
  <span style="color:var(--text)">{{ result.patient_id }}</span>
</div>

<div class="page-header">
  <h1>Eligibility Screening</h1>
  <p>{{ result.patient_id }} · {{ result.trial_name }}</p>
</div>

<!-- Eligibility banner -->
<div class="result-banner {{ 'eligible' if result.eligible else 'ineligible' }}">
  <div class="result-icon">
    {{ '✅' if result.eligible else '❌' }}
  </div>
  <div>
    <div class="result-title">
      {{ 'PRE-SCREENING CANDIDATE' if result.eligible else 'DOES NOT MEET CRITERIA' }}
    </div>
    <div class="result-subtitle">{{ result.summary }}</div>
  </div>
</div>

<!-- AI Narrative -->
<div class="section-header">
  <h2>🤖 AI Clinical Narrative</h2>
  <span class="pill pill-ghost" style="font-size:11px;">Mocked · No Bedrock call</span>
</div>
<div class="narrative">{{ result.narrative }}</div>

<!-- Patient metrics snapshot -->
<div class="section-header">
  <h2>📊 Patient CGM Snapshot</h2>
</div>
<div class="cards-grid" style="grid-template-columns: repeat(auto-fill, minmax(150px,1fr));gap:12px;margin-bottom:24px;">
  {% set m = result.metrics %}
  {% set color = tir_color(m.tir_percent) %}
  <div class="metric">
    <div class="metric-label">TIR%</div>
    <div class="metric-value {{ color }}">{{ '%.1f'|format(m.tir_percent) }}%</div>
    <div class="metric-sub">Target ≥ 70%</div>
  </div>
  <div class="metric">
    <div class="metric-label">GMI</div>
    <div class="metric-value">{{ '%.1f'|format(m.gmi) }}%</div>
    <div class="metric-sub">Est. HbA1c</div>
  </div>
  <div class="metric">
    <div class="metric-label">Mean Glucose</div>
    <div class="metric-value">{{ '%.0f'|format(m.mean_glucose) }}</div>
    <div class="metric-sub">mg/dL</div>
  </div>
  <div class="metric">
    <div class="metric-label">CV%</div>
    <div class="metric-value">{{ '%.1f'|format(m.cv_percent) }}%</div>
    <div class="metric-sub">Variability</div>
  </div>
  <div class="metric">
    <div class="metric-label">Hypos</div>
    <div class="metric-value {{ 'red' if m.hypo_events > 3 else ('amber' if m.hypo_events > 0 else 'green') }}">{{ m.hypo_events }}</div>
    <div class="metric-sub">/day &lt; 70 mg/dL</div>
  </div>
  <div class="metric">
    <div class="metric-label">Severe Hypos</div>
    <div class="metric-value {{ 'red' if m.severe_hypo_events > 0 else 'green' }}">{{ m.severe_hypo_events }}</div>
    <div class="metric-sub">/day &lt; 54 mg/dL</div>
  </div>
</div>

<!-- Criterion table -->
<div class="section-header">
  <h2>📋 Inclusion Criteria</h2>
  <span class="section-count">{{ result.inc_results|length }} criteria</span>
</div>
<div class="table-wrap" style="margin-bottom:24px;">
  <table>
    <thead>
      <tr>
        <th>Status</th><th>Criterion</th><th>Patient Value</th>
        <th>Required</th><th>Clinical Rationale</th>
      </tr>
    </thead>
    <tbody>
      {% for r in result.inc_results %}
      <tr>
        <td>
          {% if r.passed %}
          <span class="pill pill-green">✓ Pass</span>
          {% else %}
          <span class="pill pill-red">✗ Fail</span>
          {% endif %}
        </td>
        <td>
          <strong>{{ r.human_label }}</strong>
          <div style="font-size:11px;color:var(--subtext)">{{ r.field }}</div>
        </td>
        <td>
          <span class="{{ 'check' if r.passed else 'cross' }}">
            {{ '%.2f'|format(r.patient_value) if r.patient_value is not none else '—' }}
          </span>
        </td>
        <td style="white-space:nowrap">{{ r.operator }} {{ r.threshold }}</td>
        <td style="font-size:12px;color:var(--subtext);max-width:280px">{{ r.human_rationale }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>

<div class="section-header">
  <h2>🚫 Exclusion Criteria</h2>
  <span class="section-count">{{ result.exc_results|length }} criteria</span>
</div>
<div class="table-wrap" style="margin-bottom:24px;">
  <table>
    <thead>
      <tr>
        <th>Status</th><th>Criterion</th><th>Patient Value</th>
        <th>Trigger Threshold</th><th>Clinical Rationale</th>
      </tr>
    </thead>
    <tbody>
      {% for r in result.exc_results %}
      <tr>
        <td>
          {% if r.triggered %}
          <span class="pill pill-red">⚠ Triggered</span>
          {% else %}
          <span class="pill pill-green">✓ Clear</span>
          {% endif %}
        </td>
        <td>
          <strong>{{ r.human_label }}</strong>
          <div style="font-size:11px;color:var(--subtext)">{{ r.field }}</div>
        </td>
        <td>
          <span class="{{ 'cross' if r.triggered else 'check' }}">
            {{ '%.2f'|format(r.patient_value) if r.patient_value is not none else '—' }}
          </span>
        </td>
        <td style="white-space:nowrap">{{ r.operator }} {{ r.threshold }}</td>
        <td style="font-size:12px;color:var(--subtext);max-width:280px">{{ r.human_rationale }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>

<!-- Other trials quick links -->
<div class="section-header">
  <h2>🔁 Other Trials for this Patient</h2>
</div>
<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px;">
  {% for trial in all_trials %}
  {% if trial.trial_id != result.trial_id %}
  {% set other = screen(result.patient_id, result.metrics, trial) %}
  <a class="btn {{ 'btn-primary' if other.eligible else 'btn-ghost' }}"
     href="/screen/{{ result.patient_id }}/{{ trial.trial_id }}">
    {{ '✓' if other.eligible else '✗' }} {{ trial.trial_name[:30] }}
  </a>
  {% endif %}
  {% endfor %}
</div>
"""


@app.route("/screen/<patient_id>/<trial_id>")
def screen_view(patient_id, trial_id):
    patient = _PATIENT_INDEX.get(patient_id)
    trial = _TRIAL_INDEX.get(trial_id)
    if not patient or not trial:
        abort(404)

    result = screen_patient(patient_id, patient, trial)

    content = render_template_string(
        SCREEN_TEMPLATE,
        result=result,
        all_trials=DEMO_TRIALS,
        screen=lambda pid, m, t: screen_patient(pid, m, t),
        tir_color=tir_color,
    )
    return render_page(
        f"{patient_id} × {trial_id}",
        "",
        content
    )


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTE: GET /cohort/<trial_id>  — Cohort vs Trial
# ─────────────────────────────────────────────────────────────────────────────

COHORT_TEMPLATE = """
<div class="breadcrumb">
  <a href="/">Dashboard</a>
  <span>/</span>
  <a href="/trials">Trials</a>
  <span>/</span>
  <span style="color:var(--text)">{{ trial.trial_name }}</span>
</div>

<div class="page-header">
  <h1>Cohort Screening — {{ trial.trial_name }}</h1>
  <p>{{ eligible_count }} of {{ results|length }} patients meet pre-screening criteria ·
     Ranked by eligibility then TIR%</p>
</div>

<!-- Summary stats row -->
<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-bottom:28px;">
  <div class="metric">
    <div class="metric-label">Total Screened</div>
    <div class="metric-value">{{ results|length }}</div>
    <div class="metric-sub">patients in cohort</div>
  </div>
  <div class="metric">
    <div class="metric-label">Eligible</div>
    <div class="metric-value green">{{ eligible_count }}</div>
    <div class="metric-sub">pre-screening candidates</div>
  </div>
  <div class="metric">
    <div class="metric-label">Not Eligible</div>
    <div class="metric-value red">{{ results|length - eligible_count }}</div>
    <div class="metric-sub">excluded at pre-screen</div>
  </div>
  <div class="metric">
    <div class="metric-label">Yield Rate</div>
    <div class="metric-value {{ 'green' if yield_rate >= 50 else 'amber' }}">
      {{ '%.0f'|format(yield_rate) }}%
    </div>
    <div class="metric-sub">cohort match rate</div>
  </div>
  <div class="metric">
    <div class="metric-label">Trial Phase</div>
    <div class="metric-value" style="font-size:22px;">{{ trial.phase }}</div>
    <div class="metric-sub">{{ trial.sponsor[:28] }}…</div>
  </div>
  <div class="metric">
    <div class="metric-label">Criteria</div>
    <div class="metric-value">{{ trial.inclusion_criteria|length + trial.exclusion_criteria|length }}</div>
    <div class="metric-sub">{{ trial.inclusion_criteria|length }} inc · {{ trial.exclusion_criteria|length }} exc</div>
  </div>
</div>

<!-- Ranked results table -->
<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th>Rank</th>
        <th>Patient</th>
        <th>Status</th>
        <th>TIR%</th>
        <th>GMI</th>
        <th>Mean Gluc</th>
        <th>Hypos</th>
        <th>CV%</th>
        <th>Criteria Met</th>
        <th>Action</th>
      </tr>
    </thead>
    <tbody>
      {% for res in results %}
      {% set m = res.metrics %}
      {% set color = tir_color(m.tir_percent) %}
      <tr>
        <td>
          <span class="rank-badge {{ 'top' if res.eligible }}">{{ loop.index }}</span>
        </td>
        <td>
          <strong>{{ res.patient_id }}</strong>
          <div style="font-size:11px;color:var(--subtext)">
            {{ m.age_group }} · {{ m.diabetes_type }}
          </div>
        </td>
        <td>
          {% if res.eligible %}
          <span class="pill pill-green">✓ Candidate</span>
          {% else %}
          <span class="pill pill-red">✗ Excluded</span>
          {% endif %}
        </td>
        <td><span class="pill pill-{{ color }}">{{ '%.1f'|format(m.tir_percent) }}%</span></td>
        <td>{{ '%.1f'|format(m.gmi) }}%</td>
        <td>{{ '%.0f'|format(m.mean_glucose) }} mg/dL</td>
        <td>
          {{ m.hypo_events }}
          {% if m.severe_hypo_events > 0 %}
          <span style="color:var(--red);font-size:11px;">({{ m.severe_hypo_events }} severe)</span>
          {% endif %}
        </td>
        <td>{{ '%.1f'|format(m.cv_percent) }}%</td>
        <td>
          {% set passed = res.inc_results|selectattr('passed')|list|length %}
          {% set total_inc = res.inc_results|length %}
          {% set exc_clear = res.exc_results|rejectattr('triggered')|list|length %}
          {% set total_exc = res.exc_results|length %}
          <span class="{{ 'check' if res.eligible else 'cross' }}">
            {{ passed }}/{{ total_inc }} inc
          </span>
          ·
          <span class="{{ 'check' if exc_clear == total_exc else 'cross' }}">
            {{ exc_clear }}/{{ total_exc }} exc clear
          </span>
        </td>
        <td>
          <a class="btn {{ 'btn-primary' if res.eligible else 'btn-ghost' }}"
             style="padding:5px 12px;font-size:12px;"
             href="/screen/{{ res.patient_id }}/{{ trial.trial_id }}">
            View →
          </a>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>

<!-- Other trials quick nav -->
<div class="section-header" style="margin-top:32px;">
  <h2>Other Trials</h2>
</div>
<div style="display:flex;gap:10px;flex-wrap:wrap;">
  {% for t in all_trials %}
  {% if t.trial_id != trial.trial_id %}
  <a class="btn btn-ghost" href="/cohort/{{ t.trial_id }}">
    🧬 {{ t.trial_name[:32] }}
  </a>
  {% endif %}
  {% endfor %}
</div>
"""


@app.route("/cohort/<trial_id>")
def cohort_view(trial_id):
    trial = _TRIAL_INDEX.get(trial_id)
    if not trial:
        abort(404)

    # Screen all patients
    results = []
    for patient in DEMO_COHORT:
        res = screen_patient(patient["patient_id"], patient, trial)
        results.append(res)

    # Rank: eligible first, then by TIR% descending
    results.sort(key=lambda r: (not r["eligible"], -r["metrics"]["tir_percent"]))

    eligible_count = sum(1 for r in results if r["eligible"])
    yield_rate = (eligible_count / len(results) * 100) if results else 0

    content = render_template_string(
        COHORT_TEMPLATE,
        trial=trial,
        results=results,
        eligible_count=eligible_count,
        yield_rate=yield_rate,
        all_trials=DEMO_TRIALS,
        tir_color=tir_color,
    )
    return render_page(
        f"Cohort · {trial['trial_name']}",
        "trials",
        content
    )


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("╔══════════════════════════════════════════════════════╗")
    print("║  GlucoFlow Web Dashboard — Demo Mode                ║")
    print("║  http://localhost:5050                               ║")
    print("║                                                      ║")
    print("║  Routes:                                             ║")
    print("║    /                          Cohort dashboard       ║")
    print("║    /trials                    Trial protocol cards   ║")
    print("║    /screen/<patient>/<trial>  Eligibility detail     ║")
    print("║    /cohort/<trial>            Cohort screening table ║")
    print("╚══════════════════════════════════════════════════════╝")
    app.run(host="0.0.0.0", port=5050, debug=True)
