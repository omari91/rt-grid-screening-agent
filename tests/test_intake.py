"""Unit tests for resilient intake extraction, Pydantic cross-field bounce-back, and air-gap fallback."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from grid_screening_agent.intake import (
    GridConnectionIntent,
    extract_grid_intent,
    parse_intent_airgapped,
)


def test_grid_connection_intent_rejects_oversized_lv_and_mv_capacities() -> None:
    with pytest.raises(ValueError, match="too large for LV"):
        GridConnectionIntent(capacity_kw=1200.0, voltage_level="LV", asset_type="generator")

    with pytest.raises(ValueError, match="exceeds 11-20 kV MV"):
        GridConnectionIntent(capacity_kw=50_000.0, voltage_level="MV", asset_type="load")


def test_extract_grid_intent_bounces_validation_error_back_to_llm_and_self_corrects() -> None:
    """Attempt 1 wrongly assigns 1.2 MW to LV; Pydantic bounces error; Attempt 2 upgrades to MV."""
    replies = [
        '{"capacity_kw": 1200.0, "voltage_level": "LV", "asset_type": "generator"}',
        '{"capacity_kw": 1200.0, "voltage_level": "MV", "asset_type": "generator"}',
    ]
    captured_messages: list[list[dict[str, str]]] = []

    mock_client = MagicMock()

    def _fake_create(**kwargs):
        captured_messages.append(list(kwargs["messages"]))
        text = replies.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    mock_client.chat.completions.create.side_effect = _fake_create

    with patch.dict(os.environ, {"GRID_AGENT_AIRGAPPED": "false"}, clear=False):
        intent = extract_grid_intent("I need to connect a 1.2 MW solar farm.", client=mock_client)

    assert intent.capacity_kw == 1200.0
    assert intent.voltage_level == "MV"
    assert intent.asset_type == "generator"
    assert len(captured_messages) == 2
    assert "too large for LV" in captured_messages[1][-1]["content"]

    app = intent.to_connection_application()
    assert app.network_id == "Aus_MV_11kV_Suburban"
    assert app.proposed_capacity_kw == 1200.0


def test_extract_grid_intent_airgapped_and_outage_fallback() -> None:
    mock_client = MagicMock()
    with patch.dict(os.environ, {"GRID_AGENT_AIRGAPPED": "true"}, clear=False):
        intent = extract_grid_intent("We are installing 300 kVA of batteries", client=mock_client)
    mock_client.chat.completions.create.assert_not_called()
    assert intent.capacity_kw == 300.0
    assert intent.asset_type == "bess"
    assert intent.voltage_level == "MV"

    # Simulate total LLM API outage -> must fall back to deterministic regex parser seamlessly
    failing_client = MagicMock()
    failing_client.chat.completions.create.side_effect = RuntimeError("503 Service Unavailable")
    with patch.dict(os.environ, {"GRID_AGENT_AIRGAPPED": "false"}, clear=False):
        fallback_intent = extract_grid_intent(
            "Screen a 150 MW hyperscale data centre at Broken Hill 132 kV",
            client=failing_client,
        )
    assert fallback_intent.capacity_kw == 150_000.0
    assert fallback_intent.voltage_level == "HV"
    assert fallback_intent.network_id == "BrokenHill_132kV"


def test_parse_intent_airgapped_rejects_unquantified_prompt() -> None:
    with pytest.raises(ValueError, match="numerical capacity"):
        parse_intent_airgapped("Connect a huge solar system next week")

