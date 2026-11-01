"""State-compression fingerprint deduplication and interconnection queue backlog tracker.

Why this module exists:
    Utility connection teams and autonomous MCP agents routinely evaluate bursts of
    near-identical or repeated configurations when sweeping interconnection queues
    (e.g., an LLM re-querying a feeder node or two developers submitting identical
    capacity inquiries at the same Point of Common Coupling). Re-spawning child
    OpenDSS solver processes for bit-identical electrical configurations wastes CPU
    cycles and stalls queue clearance.

    By compressing the normalized electrical assessment state (excluding per-request
    UUIDs and wall-clock timeouts) into a canonical SHA-256 `queue_fingerprint`, the
    engine intercepts duplicate physics evaluations in <0.1 ms while issuing a fresh
    per-request `AuditReceipt` whose `reproducibility_sha256` remains 100% identical
    to an uncached solver run (zero numerical drift).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict, deque
from typing import Any, Optional

from grid_screening_agent.models import (
    ClearanceDecision,
    ConnectionApplication,
    ScreeningVerdict,
)


def compute_queue_fingerprint(app: ConnectionApplication, force_tier2: bool = False) -> str:
    """Compute a deterministic state-compression SHA-256 fingerprint for queue deduplication.

    Why `application_id` and `solver_timeout_sec` are excluded:
        Two connection applications requesting the exact same kW, asset class,
        inverter droop mode, seasonal scope, and bus on the same feeder represent
        identical physical boundary-value problems regardless of their tracking UUID
        or process watchdog timeout.
    """
    state_payload = {
        "application": app.model_dump(exclude={"application_id", "solver_timeout_sec"}),
        "force_tier2": bool(force_tier2),
    }
    serialized = json.dumps(state_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


class InterconnectionQueueTracker:
    """Thread-safe LRU state-compression cache and interconnection backlog telemetry monitor."""

    def __init__(self, max_entries: int = 256, max_history: int = 50) -> None:
        self._max_entries = max(1, max_entries)
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._verdicts: OrderedDict[str, ScreeningVerdict] = OrderedDict()
        self._trajectories: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._alias_to_fp: OrderedDict[str, str] = OrderedDict()
        self._history: deque[dict[str, Any]] = deque(maxlen=max_history)
        self._total_handled = 0
        self._duplicates_intercepted = 0
        self._fast_track_count = 0
        self._conditional_count = 0
        self._human_review_count = 0

    @staticmethod
    def is_cache_enabled() -> bool:
        """Return False when `GRID_AGENT_DISABLE_CACHE=1` forces cold solver execution."""
        return os.environ.get("GRID_AGENT_DISABLE_CACHE", "").strip().lower() not in (
            "1",
            "true",
            "yes",
            "on",
        )

    def lookup(self, fingerprint: str) -> Optional[dict[str, Any]]:
        """Look up a previously verified deterministic physics bundle by state fingerprint."""
        if not self.is_cache_enabled():
            return None
        with self._lock:
            entry = self._cache.get(fingerprint)
            if entry is None:
                return None
            self._cache.move_to_end(fingerprint)
            return dict(entry)

    def store_and_record(
        self,
        fingerprint: str,
        verdict: ScreeningVerdict,
        physics_bundle: Optional[dict[str, Any]] = None,
        *,
        cache_hit: bool = False,
    ) -> None:
        """Store verified physics bundle (on cold deterministic runs) and record queue audit metrics."""
        with self._lock:
            if physics_bundle is not None and self.is_cache_enabled():
                self._cache[fingerprint] = dict(physics_bundle)
                self._cache.move_to_end(fingerprint)
                while len(self._cache) > self._max_entries:
                    self._cache.popitem(last=False)

            # Always retain off-context verdict & trajectory artifacts for MCP Resource URI resolution
            self._verdicts[fingerprint] = verdict
            self._verdicts.move_to_end(fingerprint)
            while len(self._verdicts) > self._max_entries:
                self._verdicts.popitem(last=False)

            for alias in (fingerprint[:16], verdict.audit_receipt.receipt_id):
                if alias:
                    self._alias_to_fp[alias] = fingerprint
                    self._alias_to_fp.move_to_end(alias)
            while len(self._alias_to_fp) > self._max_entries * 2:
                self._alias_to_fp.popitem(last=False)

            traj_pts = (
                [pt.model_dump() for pt in verdict.tier2.voltage_profile]
                if verdict.tier2 is not None
                else []
            )
            self._trajectories[fingerprint] = {
                "fingerprint": fingerprint,
                "receipt_id": verdict.audit_receipt.receipt_id,
                "reproducibility_sha256": verdict.audit_receipt.reproducibility_sha256,
                "network_id": verdict.connection_point.network_id,
                "connection_node": verdict.connection_point.connection_node,
                "decision": verdict.decision.value,
                "study_scope": verdict.tier2.study_scope if verdict.tier2 is not None else "tier1_analytical_only",
                "points_count": len(traj_pts),
                "voltage_profile": traj_pts,
            }
            self._trajectories.move_to_end(fingerprint)
            while len(self._trajectories) > self._max_entries:
                self._trajectories.popitem(last=False)

            self._total_handled += 1
            if cache_hit:
                self._duplicates_intercepted += 1
            if verdict.decision == ClearanceDecision.FAST_TRACK_APPROVED:
                self._fast_track_count += 1
            elif verdict.decision == ClearanceDecision.CONDITIONAL_DOE_OR_CAP:
                self._conditional_count += 1
            if verdict.requires_human_review:
                self._human_review_count += 1

            peak_v = (
                verdict.tier2.peak_voltage_pu
                if verdict.tier2 is not None
                else verdict.tier1.predicted_peak_v_pu
            )
            loading_pct = (
                verdict.tier2.max_line_loading_pct
                if verdict.tier2 is not None
                else verdict.tier1.estimated_thermal_loading_pct
            )
            self._history.appendleft(
                {
                    "receipt_id": verdict.audit_receipt.receipt_id,
                    "timestamp_utc": verdict.audit_receipt.timestamp_utc,
                    "application_id": verdict.application.application_id,
                    "queue_fingerprint": fingerprint[:16],
                    "reproducibility_sha256": verdict.audit_receipt.reproducibility_sha256,
                    "network_id": verdict.connection_point.network_id,
                    "connection_node": verdict.connection_point.connection_node,
                    "nominal_kv": verdict.connection_point.nominal_kv,
                    "asset_type": verdict.application.asset_type.value,
                    "proposed_capacity_kw": verdict.application.proposed_capacity_kw,
                    "peak_voltage_pu": peak_v,
                    "max_loading_pct": loading_pct,
                    "scr_n1": verdict.tier1.scr_n1,
                    "decision": verdict.decision.value,
                    "binding_constraints": [c.value for c in verdict.binding_constraints],
                    "requires_human_review": verdict.requires_human_review,
                    "regulatory_override": verdict.regulatory_override,
                    "cache_hit": cache_hit,
                }
            )

    def snapshot_stats(self) -> dict[str, Any]:
        """Return thread-safe snapshot of interconnection queue backlog metrics and audit trail."""
        with self._lock:
            return {
                "total_screenings_handled": self._total_handled,
                "fast_track_clearances": self._fast_track_count,
                "conditional_envelopes_issued": self._conditional_count,
                "backlog_duplicates_intercepted": self._duplicates_intercepted,
                "escalated_to_human_review": self._human_review_count,
                "active_fingerprints_cached": len(self._cache),
                "recent_audit_entries": list(self._history),
            }

    def store_trajectory(self, fingerprint: str, payload: dict[str, Any]) -> None:
        """Park a standalone Tier-2 trajectory payload for off-context `grid://verdicts/.../trajectory` reads."""
        with self._lock:
            self._trajectories[fingerprint] = dict(payload)
            self._trajectories.move_to_end(fingerprint)
            if len(fingerprint) > 16:
                self._alias_to_fp[fingerprint[:16]] = fingerprint
                self._alias_to_fp.move_to_end(fingerprint[:16])
            while len(self._trajectories) > self._max_entries:
                self._trajectories.popitem(last=False)

    def get_trajectory(self, identifier: str) -> Optional[dict[str, Any]]:
        """Resolve a parked trajectory by full `queue_fingerprint`, 16-char prefix, or `receipt_id`."""
        key = identifier.strip()
        with self._lock:
            fp = self._alias_to_fp.get(key, key)
            item = self._trajectories.get(fp)
            return dict(item) if item is not None else None

    def get_verdict(self, identifier: str) -> Optional[ScreeningVerdict]:
        """Resolve a parked `ScreeningVerdict` by `queue_fingerprint`, 16-char prefix, or `receipt_id`."""
        key = identifier.strip()
        with self._lock:
            fp = self._alias_to_fp.get(key, key)
            return self._verdicts.get(fp)

    def clear(self) -> None:
        """Reset cached fingerprints and counters (used in isolated unit tests)."""
        with self._lock:
            self._cache.clear()
            self._verdicts.clear()
            self._trajectories.clear()
            self._alias_to_fp.clear()
            self._history.clear()
            self._total_handled = 0
            self._duplicates_intercepted = 0
            self._fast_track_count = 0
            self._conditional_count = 0
            self._human_review_count = 0


INTERCONNECTION_QUEUE = InterconnectionQueueTracker(
    max_entries=max(16, int(os.environ.get("GRID_AGENT_QUEUE_CACHE_SIZE", "256")))
)

