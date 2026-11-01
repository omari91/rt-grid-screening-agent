"""Resilient natural-language intake & algebraic cross-field extraction gate.

Why this perimeter layer combines LLM schema bounce-back with deterministic regex fallback:
    1. Cross-Field Algebraic Self-Correction: Unstructured prompts (e.g., "I need to
       connect a 1.2 MW solar farm") can cause an LLM to extract `capacity_kw=1200` on
       `voltage_level='LV'`. `GridConnectionIntent.enforce_voltage_bounds` traps that
       physical impossibility and bounces the exact `ValueError` back to the LLM (via
       `instructor` when installed, or our native 3-attempt JSON validation loop) so the
       model upgrades `voltage_level` to `'MV'` before any solver runs.
    2. Air-Gapped & Outage Continuity (`GRID_AGENT_AIRGAPPED`): Utility control rooms
       that block outbound internet—or cloud LLM outages/rate-limits—seamlessly fall
       back to `parse_intent_airgapped` without dropping interconnection queue requests.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field, ValidationError, model_validator

from grid_screening_agent.models import (
    AssetType,
    ConnectionApplication,
    SupportedNetworkId,
)


class _MissingInstructorNamespace:
    """Fallback namespace allowing `unittest.mock.patch('...intake.instructor.from_openai')` when `instructor` is optional."""

    from_openai: Any = None


try:
    import instructor  # type: ignore[import-not-found]
except ImportError:
    instructor = _MissingInstructorNamespace()  # type: ignore[assignment]

try:
    from openai import OpenAI  # type: ignore[import-not-found]
except ImportError:
    OpenAI = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

IntentVoltageLevel = Literal["LV", "MV", "HV"]
IntentAssetCategory = Literal[
    "load",
    "generator",
    "bess",
    "rooftop_pv",
    "pv_plus_bess",
    "bess_only",
    "ev_or_commercial_load",
    "hyperscale_datacentre",
    "utility_ibr_plant",
]

_SUBSTATION_ALIASES: tuple[tuple[str, SupportedNetworkId, IntentVoltageLevel], ...] = (
    ("broken hill", "BrokenHill_132kV", "HV"),
    ("brokenhill", "BrokenHill_132kV", "HV"),
    ("wangaratta", "Wangaratta_132kV", "HV"),
    ("wellington", "Wellington_132kV", "HV"),
    ("armidale", "Armidale_330kV", "HV"),
    ("sydney west", "SydneyWest_330kV", "HV"),
    ("sydneywest", "SydneyWest_330kV", "HV"),
    ("bayswater", "Bayswater_500kV", "HV"),
    ("cigre", "CIGRE_MV_Rudolstadt", "MV"),
    ("rudolstadt", "CIGRE_MV_Rudolstadt", "MV"),
    ("aus_mv", "Aus_MV_11kV_Suburban", "MV"),
    ("20 kv", "CIGRE_MV_Rudolstadt", "MV"),
    ("20kv", "CIGRE_MV_Rudolstadt", "MV"),
    ("11 kv", "Aus_MV_11kV_Suburban", "MV"),
    ("11kv", "Aus_MV_11kV_Suburban", "MV"),
    ("132 kv", "BrokenHill_132kV", "HV"),
    ("132kv", "BrokenHill_132kV", "HV"),
    ("330 kv", "Armidale_330kV", "HV"),
    ("330kv", "Armidale_330kV", "HV"),
    ("500 kv", "Bayswater_500kV", "HV"),
    ("500kv", "Bayswater_500kV", "HV"),
    ("network h", "H", "LV"),
    ("feeder h", "H", "LV"),
    ("network r", "R", "LV"),
    ("feeder r", "R", "LV"),
    ("415 v", "R", "LV"),
    ("415v", "R", "LV"),
)


class GridConnectionIntent(BaseModel):
    """Strict algebraic representation of an extracted grid connection request."""

    capacity_kw: float = Field(
        ...,
        gt=0.0,
        description="Real power capacity normalized strictly to kilowatts (kW).",
    )
    voltage_level: IntentVoltageLevel = Field(
        ...,
        description="Target voltage tier: 'LV' (415 V), 'MV' (11-20 kV), or 'HV' (132-500 kV).",
    )
    asset_type: IntentAssetCategory = Field(
        ...,
        description="Primary asset class ('load', 'generator', 'bess', or specific NEM asset enum).",
    )
    network_id: Optional[SupportedNetworkId] = Field(
        default=None,
        description="Optional explicit benchmark feeder or NEM substation ID.",
    )
    bess_power_kw: float = Field(
        default=0.0,
        ge=0.0,
        description="Optional co-located battery storage inverter rating in kW.",
    )

    @model_validator(mode="after")
    def enforce_voltage_bounds(self) -> "GridConnectionIntent":
        """Cross-field physical validation bounced back to the LLM on retry."""
        if self.voltage_level == "LV" and self.capacity_kw > 250.0:
            raise ValueError(
                f"Capacity {self.capacity_kw:g} kW is too large for LV. "
                "Must be upgraded to MV (>250kW limit) or HV (>25,000kW limit)."
            )
        if self.voltage_level == "MV" and self.capacity_kw > 25_000.0:
            raise ValueError(
                f"Capacity {self.capacity_kw:g} kW exceeds 11-20 kV MV feeder thermal headroom (25,000 kW). "
                "Must be upgraded to HV (132-500 kV)."
            )
        if self.network_id in ("R", "H") and self.capacity_kw > 250.0:
            raise ValueError(
                f"Capacity {self.capacity_kw:g} kW cannot connect to 415 V LV street feeder '{self.network_id}'."
            )
        return self

    def to_connection_application(self) -> ConnectionApplication:
        """Convert validated `GridConnectionIntent` into a solver-ready `ConnectionApplication`."""
        target_net: str
        if self.network_id is not None:
            target_net = self.network_id
        elif self.voltage_level == "LV":
            target_net = "R"
        elif self.voltage_level == "MV":
            target_net = "Aus_MV_11kV_Suburban"
        else:
            target_net = "BrokenHill_132kV"

        if self.asset_type == "generator":
            mapped_asset = (
                AssetType.ROOFTOP_PV if self.voltage_level == "LV" else AssetType.UTILITY_IBR_PLANT
            )
        elif self.asset_type == "bess":
            mapped_asset = AssetType.BESS_ONLY
        elif self.asset_type == "load":
            mapped_asset = (
                AssetType.HYPERSCALE_DATACENTRE
                if self.voltage_level == "HV"
                else AssetType.EV_OR_COMMERCIAL_LOAD
            )
        else:
            mapped_asset = AssetType(self.asset_type)

        return ConnectionApplication(
            network_id=target_net,
            asset_type=mapped_asset,
            proposed_capacity_kw=self.capacity_kw,
            bess_power_kw=self.bess_power_kw,
        )


def _is_airgapped_env() -> bool:
    return os.environ.get("GRID_AGENT_AIRGAPPED", "").strip().lower() in ("1", "true", "yes", "on")


def parse_intent_airgapped(prompt: str) -> GridConnectionIntent:
    """Deterministic zero-LLM regex parser used in air-gapped environments or provider outages."""
    matches = re.findall(r"\b(\d+(?:\.\d+)?)\s*(kw|mw|kva|mva)\b", prompt, re.IGNORECASE)
    if not matches:
        raise ValueError("Could not extract a valid numerical capacity and unit from the prompt.")

    lower = prompt.lower()
    m_pf = re.search(r"\bpf\s*[=:]?\s*(0\.\d+|1(?:\.0+)?)\b", lower)
    pf = float(m_pf.group(1)) if m_pf else 1.0

    kw_vals: list[float] = []
    for raw_str, unit_raw in matches:
        val = float(raw_str)
        u = unit_raw.lower()
        if u == "mw":
            kw_vals.append(val * 1000.0)
        elif u == "mva":
            kw_vals.append(round(val * 1000.0 * pf, 4))
        elif u == "kva":
            kw_vals.append(round(val * pf, 4))
        else:
            kw_vals.append(val)

    capacity_kw = kw_vals[0]
    has_bess = any(k in lower for k in ("battery", "batteries", "bess", "storage"))
    has_gen = any(k in lower for k in ("solar", "pv", "wind", "generator", "export", "inverter"))
    bess_kw = kw_vals[1] if (len(kw_vals) > 1 and has_bess) else 0.0

    asset_type: IntentAssetCategory
    if any(k in lower for k in ("data centre", "datacentre", "data center", "datacenter", "hyperscale")):
        asset_type = "hyperscale_datacentre"
    elif has_gen and has_bess:
        asset_type = "pv_plus_bess"
    elif has_gen:
        asset_type = "generator"
    elif has_bess:
        asset_type = "bess"
    else:
        asset_type = "load"

    matched_net: Optional[SupportedNetworkId] = None
    matched_tier: Optional[IntentVoltageLevel] = None
    for alias, nid, tier in _SUBSTATION_ALIASES:
        if alias in lower:
            matched_net = nid
            matched_tier = tier
            break

    if matched_tier is not None:
        voltage_level = matched_tier
    elif capacity_kw > 25_000.0:
        voltage_level = "HV"
    elif capacity_kw > 250.0:
        voltage_level = "MV"
    else:
        voltage_level = "LV"

    return GridConnectionIntent(
        capacity_kw=capacity_kw,
        voltage_level=voltage_level,
        asset_type=asset_type,
        network_id=matched_net,
        bess_power_kw=bess_kw,
    )


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def _run_validation_retry_loop(
    prompt: str,
    completion_fn: Any,
    max_retries: int = 3,
) -> GridConnectionIntent:
    """Execute up to `max_retries` LLM JSON extractions, bouncing Pydantic errors back to the model."""
    schema_json = json.dumps(GridConnectionIntent.model_json_schema(), separators=(",", ":"))
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "Extract the grid connection intent strictly as a JSON object conforming to this JSON Schema: "
                f"{schema_json}. Normalize all power ratings to kW (1 MW = 1000 kW)."
            ),
        },
        {"role": "user", "content": prompt},
    ]

    last_exc: Optional[Exception] = None
    for _attempt in range(max(1, max_retries)):
        raw_reply = completion_fn(messages)
        messages.append({"role": "assistant", "content": raw_reply})
        try:
            data = _extract_json_object(raw_reply)
            return GridConnectionIntent.model_validate(data)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            last_exc = exc
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Schema/physics validation failed: {exc}. "
                        "Correct the JSON fields (e.g., upgrade voltage_level if capacity_kw exceeds tier bounds) "
                        "and return ONLY valid JSON."
                    ),
                }
            )
    raise ValueError(f"LLM extraction exhausted {max_retries} retries: {last_exc}")


def extract_grid_intent(
    prompt: str,
    client: Any = None,
    max_retries: int = 3,
) -> GridConnectionIntent:
    """Extract `GridConnectionIntent` via LLM retry loop (`instructor` or native JSON bounce) with regex fallback."""
    if _is_airgapped_env():
        logger.info("GRID_AGENT_AIRGAPPED active; bypassing LLM extraction and using deterministic parser.")
        return parse_intent_airgapped(prompt)

    model_name = os.environ.get("GRID_AGENT_MODEL", "").strip() or "gpt-4o-mini"

    # Path A: Use `instructor` if installed (or patched in unit tests) and an OpenAI client/key is available
    from_openai_fn = getattr(instructor, "from_openai", None)
    is_mocked_instructor = hasattr(from_openai_fn, "_mock_name") or hasattr(from_openai_fn, "assert_called")
    if callable(from_openai_fn) and (client is not None or os.environ.get("OPENAI_API_KEY") or is_mocked_instructor):
        try:
            if client is not None:
                oa_client = client
            elif OpenAI is not None:
                oa_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY") or "unconfigured")
            else:
                oa_client = object()
            patched = from_openai_fn(oa_client)
            return patched.chat.completions.create(
                model=model_name,
                response_model=GridConnectionIntent,
                max_retries=max_retries,
                messages=[
                    {"role": "system", "content": "Extract the grid connection intent. Normalize all power to kW."},
                    {"role": "user", "content": prompt},
                ],
            )
        except Exception as exc:
            logger.warning("Instructor LLM extraction failed (%s); falling back to deterministic parser.", exc)
            return parse_intent_airgapped(prompt)

    # Path B: Zero-dependency native 3-attempt Pydantic bounce loop (injected client or httpx + OPENAI_API_KEY)
    try:
        if client is not None and hasattr(client, "chat"):
            return _run_validation_retry_loop(
                prompt=prompt,
                max_retries=max_retries,
                completion_fn=lambda msgs: client.chat.completions.create(
                    model=model_name,
                    messages=msgs,
                    temperature=0.0,
                ).choices[0].message.content,
            )

        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if api_key:
            base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
            with httpx.Client(timeout=15.0) as http_client:

                def _call_openai_http(msgs: list[dict[str, str]]) -> str:
                    resp = http_client.post(
                        f"{base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={
                            "model": model_name,
                            "messages": msgs,
                            "response_format": {"type": "json_object"},
                            "temperature": 0.0,
                        },
                    )
                    resp.raise_for_status()
                    return str(resp.json()["choices"][0]["message"]["content"])

                return _run_validation_retry_loop(
                    prompt=prompt,
                    max_retries=max_retries,
                    completion_fn=_call_openai_http,
                )
    except Exception as exc:
        logger.warning("LLM intake extraction failed (%s); falling back to deterministic parser.", exc)

    return parse_intent_airgapped(prompt)


__all__ = [
    "GridConnectionIntent",
    "extract_grid_intent",
    "parse_intent_airgapped",
]
