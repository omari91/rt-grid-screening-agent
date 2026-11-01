"""Intake-layer resilience tests: Pydantic bounce-back triggers, regex fallback, and outage/air-gap routing.

Why these live alongside `tests/test_intake.py`:
    They pin the exact deterministic-parser behaviours that the `instructor` retry loop
    relies on as its last line of defence. If an LLM provider times out or the host is
    air-gapped, these guarantees are the only thing standing between a queue request and
    a silent misclassification.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from grid_screening_agent.intake import (
    GridConnectionIntent,
    extract_grid_intent,
    parse_intent_airgapped,
)

# --- 1. PYDANTIC SCHEMA VALIDATION ---


def test_pydantic_schema_validation_passes() -> None:
    intent = GridConnectionIntent(capacity_kw=150.0, voltage_level="LV", asset_type="generator")
    assert intent.capacity_kw == 150.0
    assert intent.voltage_level == "LV"
    assert intent.asset_type == "generator"


def test_pydantic_schema_validation_fails_lv_limit() -> None:
    """The cross-field error message is what `instructor` bounces back to the LLM on retry."""
    with pytest.raises(ValidationError, match="is too large for LV"):
        GridConnectionIntent(capacity_kw=300.0, voltage_level="LV", asset_type="load")


# --- 2. DETERMINISTIC REGEX (AIR-GAPPED) PARSER ---


def test_airgapped_parser_extracts_load_lv() -> None:
    intent = parse_intent_airgapped("We need to connect a 150 kw load to the network.")
    assert intent.capacity_kw == 150.0
    assert intent.asset_type == "load"
    assert intent.voltage_level == "LV"


def test_airgapped_parser_extracts_bess_mv_with_unit_conversion() -> None:
    intent = parse_intent_airgapped("Installing a 1.2 MW battery energy storage system.")
    assert intent.capacity_kw == 1200.0
    assert intent.asset_type == "bess"
    assert intent.voltage_level == "MV"


def test_airgapped_parser_extracts_solar_generator_from_kva() -> None:
    intent = parse_intent_airgapped("I have a 300 kva solar array to connect.")
    assert intent.capacity_kw == 300.0
    assert intent.asset_type == "generator"
    assert intent.voltage_level == "MV"


def test_airgapped_parser_fails_on_missing_capacity() -> None:
    with pytest.raises(ValueError, match="Could not extract a valid numerical capacity"):
        parse_intent_airgapped("I want to connect a solar panel but I don't know the size yet.")


# --- 3. ORCHESTRATION & RESILIENCE ---


def test_extract_grid_intent_forces_airgapped_when_env_var_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRID_AGENT_AIRGAPPED", "true")
    intent = extract_grid_intent("Connect a 500 kw wind generator")
    assert intent.capacity_kw == 500.0
    assert intent.asset_type == "generator"
    assert intent.voltage_level == "MV"


@patch("grid_screening_agent.intake.instructor.from_openai")
def test_extract_grid_intent_llm_failure_triggers_fallback(
    mock_from_openai: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider outage inside the `instructor` path must degrade to the regex parser, never raise."""
    monkeypatch.delenv("GRID_AGENT_AIRGAPPED", raising=False)
    patched_client = MagicMock()
    patched_client.chat.completions.create.side_effect = Exception("OpenAI API Timeout")
    mock_from_openai.return_value = patched_client

    intent = extract_grid_intent("We are proposing a 750 kw BESS site.")

    mock_from_openai.assert_called_once()
    assert intent.capacity_kw == 750.0
    assert intent.asset_type == "bess"
    assert intent.voltage_level == "MV"

