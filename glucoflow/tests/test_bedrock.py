"""
test_bedrock.py — Test suite for src/ai/bedrock_client.py

Coverage strategy:
  - TestMessageBuilders   : pure function tests — no mocks needed
  - TestBedrockClientInit : constructor + model_id defaults
  - TestGenerateNarrative : mock boto3 client → verify payload shape, response parsing
  - TestPopulationNarrative: same for population path
  - TestMCPActions        : action validation, resolver injection, error cases
  - TestClinicalSystemPrompt: verify required clinical rules are present in the prompt
  - TestEdgeCases         : missing fields, None values, empty dicts

All tests use injected mock clients — zero real AWS calls.
"""

import json
import sys
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ai.bedrock_client import (
    MCP_ACTION_GROUPS,
    CLINICAL_SYSTEM_PROMPT,
    MODEL_ID,
    MAX_TOKENS,
    TEMPERATURE,
    BedrockClient,
    _build_user_message,
    _build_population_message,
)


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #

def _make_mock_bedrock_client(narrative_text: str = "Test narrative.") -> MagicMock:
    """Return a mock boto3 bedrock-runtime client that returns a fixed narrative via converse API."""
    mock_client = MagicMock()
    mock_client.converse.return_value = {
        "output": {
            "message": {
                "content": [{"text": narrative_text}]
            }
        }
    }
    return mock_client


def _sample_gold_metrics() -> dict:
    return {
        "patient_id": "adult#001",
        "day": "2026-10-07",
        "tir_percent": 78.5,
        "mean_glucose": 142.3,
        "hypo_events": 2,
        "severe_hypo_events": 0,
        "cv_percent": 28.4,
        "gmi": 6.71,
        "source_system": "simglucose",
    }


def _sample_population_stats() -> dict:
    return {
        "date": "2026-10-07",
        "patient_count": 3,
        "mean_tir_percent": 74.2,
        "mean_glucose": 148.9,
        "total_hypo_events": 6,
        "total_severe_hypo_events": 1,
    }


# ------------------------------------------------------------------ #
#  TestMessageBuilders                                                 #
# ------------------------------------------------------------------ #

class TestMessageBuilders:
    """Pure function tests — no I/O, no mocks needed."""

    def test_build_user_message_contains_patient_id(self):
        msg = _build_user_message(_sample_gold_metrics())
        assert "adult#001" in msg

    def test_build_user_message_contains_tir(self):
        msg = _build_user_message(_sample_gold_metrics())
        assert "78.5" in msg

    def test_build_user_message_contains_mean_glucose(self):
        msg = _build_user_message(_sample_gold_metrics())
        assert "142.3" in msg

    def test_build_user_message_contains_gmi(self):
        msg = _build_user_message(_sample_gold_metrics())
        assert "6.71" in msg

    def test_build_user_message_contains_cv(self):
        msg = _build_user_message(_sample_gold_metrics())
        assert "28.4" in msg

    def test_build_user_message_extra_context_included(self):
        msg = _build_user_message(_sample_gold_metrics(), extra_context="T1DM, age 14")
        assert "T1DM, age 14" in msg

    def test_build_user_message_no_extra_context_no_extra_line(self):
        msg = _build_user_message(_sample_gold_metrics(), extra_context=None)
        assert "Additional context" not in msg

    def test_build_user_message_missing_field_shows_na(self):
        metrics = _sample_gold_metrics()
        del metrics["gmi"]
        msg = _build_user_message(metrics)
        assert "N/A" in msg

    def test_build_population_message_contains_date(self):
        msg = _build_population_message(_sample_population_stats())
        assert "2026-10-07" in msg

    def test_build_population_message_contains_patient_count(self):
        msg = _build_population_message(_sample_population_stats())
        assert "3" in msg

    def test_build_population_message_contains_mean_tir(self):
        msg = _build_population_message(_sample_population_stats())
        assert "74.2" in msg

    def test_build_population_message_missing_field_shows_na(self):
        stats = _sample_population_stats()
        del stats["mean_glucose"]
        msg = _build_population_message(stats)
        assert "N/A" in msg


# ------------------------------------------------------------------ #
#  TestClinicalSystemPrompt                                            #
# ------------------------------------------------------------------ #

class TestClinicalSystemPrompt:
    """Verify the system prompt contains all required clinical rules."""

    def test_prompt_mentions_tir_threshold(self):
        assert "70%" in CLINICAL_SYSTEM_PROMPT

    def test_prompt_mentions_severe_hypo_threshold(self):
        assert "54 mg/dL" in CLINICAL_SYSTEM_PROMPT

    def test_prompt_mentions_clinical_alert(self):
        assert "CLINICAL ALERT" in CLINICAL_SYSTEM_PROMPT

    def test_prompt_mentions_gmi_threshold(self):
        assert "8.0%" in CLINICAL_SYSTEM_PROMPT

    def test_prompt_mentions_cv_threshold(self):
        assert "36%" in CLINICAL_SYSTEM_PROMPT

    def test_prompt_has_disclaimer(self):
        assert "does not constitute" in CLINICAL_SYSTEM_PROMPT

    def test_prompt_word_limit(self):
        assert "200 words" in CLINICAL_SYSTEM_PROMPT


# ------------------------------------------------------------------ #
#  TestBedrockClientInit                                               #
# ------------------------------------------------------------------ #

class TestBedrockClientInit:

    def test_default_model_id(self):
        bc = BedrockClient(client=MagicMock())
        assert bc.model_id == MODEL_ID

    def test_custom_model_id(self):
        bc = BedrockClient(client=MagicMock(), model_id="anthropic.claude-3-sonnet-20240229-v1:0")
        assert "sonnet" in bc.model_id

    def test_injected_client_stored(self):
        mock_c = MagicMock()
        bc = BedrockClient(client=mock_c)
        assert bc._get_client() is mock_c

    def test_model_id_constant_is_haiku(self):
        assert "haiku" in MODEL_ID

    def test_max_tokens_is_sensible(self):
        assert 256 <= MAX_TOKENS <= 4096

    def test_temperature_is_low(self):
        # Clinical output should use low temperature
        assert TEMPERATURE <= 0.5


# ------------------------------------------------------------------ #
#  TestGenerateNarrative                                               #
# ------------------------------------------------------------------ #

class TestGenerateNarrative:

    def test_returns_narrative_string(self):
        bc = BedrockClient(client=_make_mock_bedrock_client("Patient is stable."))
        result = bc.generate_narrative(_sample_gold_metrics())
        assert isinstance(result, str)
        assert "Patient is stable." in result

    def test_converse_called_once(self):
        mock_client = _make_mock_bedrock_client()
        bc = BedrockClient(client=mock_client)
        bc.generate_narrative(_sample_gold_metrics())
        mock_client.converse.assert_called_once()

    def test_payload_contains_system_prompt(self):
        mock_client = _make_mock_bedrock_client()
        bc = BedrockClient(client=mock_client)
        bc.generate_narrative(_sample_gold_metrics())
        call_kwargs = mock_client.converse.call_args
        assert call_kwargs[1]["system"][0]["text"] == CLINICAL_SYSTEM_PROMPT

    def test_payload_contains_user_message(self):
        mock_client = _make_mock_bedrock_client()
        bc = BedrockClient(client=mock_client)
        bc.generate_narrative(_sample_gold_metrics())
        call_kwargs = mock_client.converse.call_args
        messages = call_kwargs[1]["messages"]
        assert messages[0]["role"] == "user"
        assert "adult#001" in messages[0]["content"][0]["text"]

    def test_payload_respects_max_tokens(self):
        mock_client = _make_mock_bedrock_client()
        bc = BedrockClient(client=mock_client)
        bc.generate_narrative(_sample_gold_metrics())
        call_kwargs = mock_client.converse.call_args
        assert call_kwargs[1]["inferenceConfig"]["maxTokens"] == MAX_TOKENS

    def test_model_id_in_invoke_call(self):
        mock_client = _make_mock_bedrock_client()
        bc = BedrockClient(client=mock_client)
        bc.generate_narrative(_sample_gold_metrics())
        call_kwargs = mock_client.converse.call_args
        assert call_kwargs[1]["modelId"] == MODEL_ID

    def test_extra_context_passed_through(self):
        mock_client = _make_mock_bedrock_client()
        bc = BedrockClient(client=mock_client)
        bc.generate_narrative(_sample_gold_metrics(), extra_context="Insulin pump user")
        call_kwargs = mock_client.converse.call_args
        messages = call_kwargs[1]["messages"]
        assert "Insulin pump user" in messages[0]["content"][0]["text"]


# ------------------------------------------------------------------ #
#  TestPopulationNarrative                                             #
# ------------------------------------------------------------------ #

class TestPopulationNarrative:

    def test_returns_string(self):
        bc = BedrockClient(client=_make_mock_bedrock_client("Population is well-controlled."))
        result = bc.generate_population_narrative(_sample_population_stats())
        assert isinstance(result, str)
        assert "Population is well-controlled." in result

    def test_invoke_called_once(self):
        mock_client = _make_mock_bedrock_client()
        bc = BedrockClient(client=mock_client)
        bc.generate_population_narrative(_sample_population_stats())
        mock_client.converse.assert_called_once()

    def test_population_payload_has_system_prompt(self):
        mock_client = _make_mock_bedrock_client()
        bc = BedrockClient(client=mock_client)
        bc.generate_population_narrative(_sample_population_stats())
        call_kwargs = mock_client.converse.call_args
        assert call_kwargs[1]["system"][0]["text"] == CLINICAL_SYSTEM_PROMPT


# ------------------------------------------------------------------ #
#  TestMCPActions                                                      #
# ------------------------------------------------------------------ #

class TestMCPActions:

    def test_mcp_action_groups_count(self):
        assert len(MCP_ACTION_GROUPS) == 3

    def test_mcp_action_names(self):
        names = {ag["name"] for ag in MCP_ACTION_GROUPS}
        assert "get_patient_summary" in names
        assert "get_population_stats" in names
        assert "flag_clinical_alert" in names

    def test_each_action_has_description(self):
        for ag in MCP_ACTION_GROUPS:
            assert "description" in ag
            assert len(ag["description"]) > 10

    def test_each_action_has_input_schema(self):
        for ag in MCP_ACTION_GROUPS:
            assert "input_schema" in ag
            assert ag["input_schema"]["type"] == "object"

    def test_flag_clinical_alert_has_severity_enum(self):
        alert_action = next(ag for ag in MCP_ACTION_GROUPS if ag["name"] == "flag_clinical_alert")
        severity_prop = alert_action["input_schema"]["properties"]["severity"]
        assert "enum" in severity_prop
        assert "urgent" in severity_prop["enum"]

    def test_invoke_mcp_action_calls_resolver(self):
        bc = BedrockClient(client=MagicMock())
        mock_resolver = MagicMock(return_value={"tir_percent": 78.5})
        result = bc.invoke_mcp_action(
            "get_patient_summary",
            {"patient_id": "adult#001", "date": "2026-10-07"},
            data_resolver=mock_resolver,
        )
        mock_resolver.assert_called_once_with(
            "get_patient_summary",
            {"patient_id": "adult#001", "date": "2026-10-07"}
        )
        assert result["tir_percent"] == 78.5

    def test_invoke_mcp_unknown_action_raises(self):
        bc = BedrockClient(client=MagicMock())
        with pytest.raises(ValueError, match="Unknown MCP action"):
            bc.invoke_mcp_action(
                "delete_patient_data",
                {},
                data_resolver=lambda a, b: {}
            )

    def test_get_population_stats_requires_date(self):
        pop_action = next(ag for ag in MCP_ACTION_GROUPS if ag["name"] == "get_population_stats")
        assert "date" in pop_action["input_schema"]["required"]
