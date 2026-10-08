"""
bedrock_client.py — GlucoFlow Bedrock AI Narrative Layer

Wraps the AWS Bedrock Runtime (Claude 3) to produce plain-English clinical
narratives from Gold-layer metrics. This is the AI brain of GlucoFlow.

Design principles:
  - Injectable boto3 client (client=None pattern) — fully testable without
    real AWS credentials, exactly like every other layer in this codebase.
  - Hard system prompt enforces clinical tone, safe-harbour disclaimers, and
    structured output format so the narrative is always predictable.
  - MCP (Model Context Protocol) action groups let the model request more
    data mid-conversation rather than receiving a single giant context dump.
  - Token budget is capped at 1024 output tokens per call — sufficient for
    a paragraph narrative, cheap at Bedrock pricing (~$0.003/call).

Why Claude 3.5 Haiku (not Sonnet/Opus)?
  - Haiku is the fastest and cheapest Claude 3.5 model on Bedrock.
  - Clinical narratives are short (< 200 words) — Haiku quality is more
    than sufficient and costs ~10x less than Sonnet per call.
  - The project can be upgraded to Sonnet/Opus by changing MODEL_ID only.
  - Updated from claude-3-haiku-20240307 (EOL) to claude-3-5-haiku-20241022.

MCP Action Groups implemented:
  1. get_patient_summary   — returns Gold metrics for one patient+day
  2. get_population_stats  — returns aggregated stats across all patients
  3. flag_clinical_alert   — surfaces a critical reading for human review
"""


import json
import logging
import os
from typing import Any, Optional
import boto3

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Model config                                                        #
# ------------------------------------------------------------------ #

MODEL_ID = "arn:aws:bedrock:us-east-2:292718260770:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0"
MAX_TOKENS = 1024
TEMPERATURE = 0.3   # Low temperature — clinical narratives should be consistent,
                    # not creative. 0.3 gives slight variation without hallucination risk.

# ------------------------------------------------------------------ #
#  Clinical system prompt                                              #
# ------------------------------------------------------------------ #

CLINICAL_SYSTEM_PROMPT = """You are GlucoFlow AI, a clinical data assistant integrated
into a continuous glucose monitoring (CGM) analytics pipeline. Your role is to translate
raw glucose metrics into clear, accurate, plain-English summaries that a diabetes care
team can act on.

RULES YOU MUST FOLLOW:
1. Always express Time-in-Range (TIR) as a percentage with one decimal place.
2. Always express mean glucose in mg/dL rounded to one decimal place.
3. Flag any TIR below 70% as suboptimal per ADA 2024 guidelines.
4. Flag any severe hypoglycaemia events (glucose < 54 mg/dL) as requiring
   immediate clinical review — use the phrase "CLINICAL ALERT" in bold.
5. Flag any GMI above 8.0% as indicating poor long-term glycaemic control.
6. Use the Coefficient of Variation (CV%): below 36% is stable, above 36% is
   high variability — state which applies.
7. Keep every narrative under 200 words.
8. End every response with this exact disclaimer on its own line:
   "⚠️ This summary is for informational purposes only and does not constitute
   medical advice. Always consult a qualified clinician."
9. Never invent data. If a metric is missing, say "data unavailable" for that field.
10. Use plain English — no jargon beyond standard diabetes care terms.
"""

# ------------------------------------------------------------------ #
#  MCP Action Group definitions                                        #
# ------------------------------------------------------------------ #

MCP_ACTION_GROUPS = [
    {
        "name": "get_patient_summary",
        "description": (
            "Retrieve the Gold-layer daily metrics for a specific patient on a "
            "specific date. Returns TIR%, mean glucose, hypo events, severe hypo "
            "events, CV%, and GMI."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "The patient identifier (e.g. 'adult#001')"
                },
                "date": {
                    "type": "string",
                    "description": "Date in YYYY-MM-DD format"
                }
            },
            "required": ["patient_id", "date"]
        }
    },
    {
        "name": "get_population_stats",
        "description": (
            "Retrieve aggregated statistics across the entire patient population "
            "for a given date. Returns mean TIR%, mean glucose, total hypo events, "
            "and patient count."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date in YYYY-MM-DD format"
                }
            },
            "required": ["date"]
        }
    },
    {
        "name": "flag_clinical_alert",
        "description": (
            "Flag a patient reading for urgent clinical review. Use when glucose "
            "drops below 54 mg/dL (severe hypoglycaemia) or when TIR% falls below "
            "50% on any single day."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "date": {"type": "string"},
                "reason": {
                    "type": "string",
                    "description": "Plain-English reason for the alert"
                },
                "severity": {
                    "type": "string",
                    "enum": ["urgent", "warning", "info"],
                    "description": "Alert severity level"
                }
            },
            "required": ["patient_id", "date", "reason", "severity"]
        }
    }
]

# ------------------------------------------------------------------ #
#  BedrockClient                                                       #
# ------------------------------------------------------------------ #

class BedrockClient:
    """
    Thin wrapper around boto3 bedrock-runtime for GlucoFlow narrative generation.

    Parameters
    ----------
    client : boto3 bedrock-runtime client, optional
        Injectable for testing. If None, a real boto3 client is created on
        first use via _get_client(). This mirrors the same pattern used in
        glue_catalog.py and athena_runner.py.
    model_id : str, optional
        Bedrock model ID. Defaults to Claude 3 Haiku.
    """

    def __init__(self, client=None, model_id: str = MODEL_ID):
        self._client = client
        self.model_id = model_id

    def _get_client(self):
        if self._client is not None:
            return self._client
        from src.common.config import cfg
        return boto3.client("bedrock-runtime", region_name="us-east-2")

    def generate_narrative(
        self,
        gold_metrics: dict[str, Any],
        extra_context: Optional[str] = None,
    ) -> str:
        """
        Generate a plain-English clinical narrative from Gold metrics.

        Parameters
        ----------
        gold_metrics : dict
            A GoldReading serialised as dict (model_dump). Must contain at least:
            patient_id, day, tir_percent, mean_glucose, hypo_events,
            severe_hypo_events, cv_percent, gmi.
        extra_context : str, optional
            Any additional context to include (e.g. "Patient is T1DM, age 14").

        Returns
        -------
        str
            The narrative text produced by Claude 3.
        """
        user_message = _build_user_message(gold_metrics, extra_context)
        client = self._get_client()
        response = client.converse(
            modelId=self.model_id,
            system=[{"text": CLINICAL_SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": user_message}]}],
            inferenceConfig={"maxTokens": MAX_TOKENS},
        )
        return response["output"]["message"]["content"][0]["text"]

    def generate_population_narrative(
        self,
        population_stats: dict[str, Any],
    ) -> str:
        """
        Generate a population-level clinical narrative from aggregated stats.

        Parameters
        ----------
        population_stats : dict
            Keys: date, patient_count, mean_tir_percent, mean_glucose,
                  total_hypo_events, total_severe_hypo_events.

        Returns
        -------
        str
            Population-level narrative text.
        """
        user_message = _build_population_message(population_stats)
        client = self._get_client()
        response = client.converse(
            modelId=self.model_id,
            system=[{"text": CLINICAL_SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": user_message}]}],
            inferenceConfig={"maxTokens": MAX_TOKENS},
        )
        return response["output"]["message"]["content"][0]["text"]

    def invoke_mcp_action(
        self,
        action_name: str,
        action_input: dict[str, Any],
        data_resolver,
    ) -> dict[str, Any]:
        """
        Execute a single MCP action group call.

        Parameters
        ----------
        action_name : str
            One of: get_patient_summary, get_population_stats, flag_clinical_alert.
        action_input : dict
            The input payload matching the action's input_schema.
        data_resolver : callable
            Function(action_name, action_input) -> dict. Injected so tests can
            return mock data without hitting real AWS/Athena.

        Returns
        -------
        dict
            The resolved action result.
        """
        # Validate action name
        valid_actions = {ag["name"] for ag in MCP_ACTION_GROUPS}
        if action_name not in valid_actions:
            raise ValueError(
                f"Unknown MCP action '{action_name}'. "
                f"Valid actions: {sorted(valid_actions)}"
            )

        logger.info(f"MCP action invoked: {action_name} with input {action_input}")
        result = data_resolver(action_name, action_input)
        logger.info(f"MCP action '{action_name}' resolved successfully.")
        return result


# ------------------------------------------------------------------ #
#  Message builders (pure functions — independently testable)         #
# ------------------------------------------------------------------ #

def _build_user_message(
    gold_metrics: dict[str, Any],
    extra_context: Optional[str] = None,
) -> str:
    """Build the user-turn message for a single-patient narrative request."""
    lines = [
        f"Please provide a clinical summary for the following patient metrics:",
        f"",
        f"Patient ID    : {gold_metrics.get('patient_id', 'unknown')}",
        f"Date          : {gold_metrics.get('day', 'unknown')}",
        f"TIR%          : {gold_metrics.get('tir_percent', 'N/A')}%",
        f"Mean Glucose  : {gold_metrics.get('mean_glucose', 'N/A')} mg/dL",
        f"Hypo Events   : {gold_metrics.get('hypo_events', 'N/A')}",
        f"Severe Hypo   : {gold_metrics.get('severe_hypo_events', 'N/A')}",
        f"CV%           : {gold_metrics.get('cv_percent', 'N/A')}%",
        f"GMI           : {gold_metrics.get('gmi', 'N/A')}%",
        f"Source        : {gold_metrics.get('source_system', 'unknown')}",
    ]
    if extra_context:
        lines += ["", f"Additional context: {extra_context}"]
    return "\n".join(lines)


def _build_population_message(population_stats: dict[str, Any]) -> str:
    """Build the user-turn message for a population-level narrative request."""
    return "\n".join([
        f"Please provide a population-level clinical summary for the following metrics:",
        f"",
        f"Date                : {population_stats.get('date', 'unknown')}",
        f"Patient Count       : {population_stats.get('patient_count', 'N/A')}",
        f"Mean TIR%           : {population_stats.get('mean_tir_percent', 'N/A')}%",
        f"Mean Glucose        : {population_stats.get('mean_glucose', 'N/A')} mg/dL",
        f"Total Hypo Events   : {population_stats.get('total_hypo_events', 'N/A')}",
        f"Total Severe Hypos  : {population_stats.get('total_severe_hypo_events', 'N/A')}",
    ])
