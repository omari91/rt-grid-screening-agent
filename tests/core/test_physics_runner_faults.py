"""Fault-tolerance tests for `execute_tier2_physics` (child-process OpenDSS boundary).

Why the child process is mocked here (and only here):
    These tests prove the *parent's* handling of pathological child outcomes — timeouts,
    segfaults (`returncode == 139`), and corrupted stdout — which cannot be reproduced
    deterministically with a real OpenDSS solve. Real AC physics remains covered by
    `tests/test_opendss_and_mitigation.py`.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from grid_screening_agent.physics._dss_runner import (
    _build_sanitized_worker_env,
    execute_tier2_physics,
)

_RUN_TARGET = "grid_screening_agent.physics._dss_runner.subprocess.run"


def test_sanitized_worker_env_strips_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-token")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG")
    monkeypatch.setenv("GRID_AGENT_API_TOKEN", "bearer-1234")

    clean_env = _build_sanitized_worker_env()

    assert clean_env["PATH"] == "/usr/bin:/bin"
    for leaked in ("OPENAI_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "GRID_AGENT_API_TOKEN"):
        assert leaked not in clean_env


@patch(_RUN_TARGET)
def test_physics_successful_execution(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=json.dumps({"intervals_solved": 17, "solver_failures": 0, "peak_voltage_pu": 1.04}).encode(),
        stderr=b"",
    )
    payload = {"network_id": "R", "proposed_capacity_kw": 15.0}

    result = execute_tier2_physics(payload)

    assert result["peak_voltage_pu"] == 1.04
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["input"] == json.dumps(payload).encode("utf-8")
    assert "OPENAI_API_KEY" not in mock_run.call_args.kwargs["env"]


@patch(_RUN_TARGET)
def test_physics_timeout_handling(mock_run: MagicMock) -> None:
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["python", "worker"], timeout=15)

    result = execute_tier2_physics({"network_id": "R", "proposed_capacity_kw": 9999.0}, timeout_seconds=15)

    assert result["decision"] == "REJECTED"
    assert result["abstention_decision"] == "ABSTAIN_DATA_OR_SOLVER_FAULT"
    assert "SOLVER_OR_DATA_FAULT" in result["binding_constraints"]
    assert result["timed_out"] is True
    assert "timed out after 15s" in result["error"]


@patch(_RUN_TARGET)
def test_physics_child_process_segfault(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(returncode=139, stdout=b"", stderr=b"Segmentation fault (core dumped)")

    result = execute_tier2_physics({"network_id": "R", "proposed_capacity_kw": 500.0})

    assert result["decision"] == "REJECTED"
    assert "SOLVER_OR_DATA_FAULT" in result["binding_constraints"]
    assert result["returncode"] == 139
    assert "Segmentation fault" in result["error"]


@patch(_RUN_TARGET)
def test_physics_corrupted_json_handling(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(
        returncode=0, stdout=b"Traceback (most recent call last): ... ValueError", stderr=b""
    )

    result = execute_tier2_physics({"network_id": "R", "proposed_capacity_kw": 50.0})

    assert result["decision"] == "REJECTED"
    assert "SOLVER_OR_DATA_FAULT" in result["binding_constraints"]
    assert "Failed to parse worker output" in result["error"]

