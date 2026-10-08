"""Product router defaults and the knobs the search is not allowed to touch."""

from __future__ import annotations

from dataclasses import asdict, fields
from datetime import UTC, datetime
from typing import Any

from app.router import RouterParams

# RouteRequest applies these on top of RouterParams(); the eval scores that path.
PRODUCT_TIME_STEP_S = 300
PRODUCT_HEADING_STEP_DEG = 7.5

# Fixed departure so every run sees the same timestamps.
DEPART = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)

# Safety constraint, not a speed knob. Sensitivity must not trial it.
FROZEN_PARAMS = frozenset({"depth_safety_margin_m"})

# Waypoint knobs do nothing on open water. They are exercised only when a case
# actually has land or an island in the way.
WAYPOINT_PARAMS = frozenset(
    {
        "waypoint_guidance",
        "waypoint_clearance_nm",
        "waypoint_corridor_margin_nm",
        "waypoint_max_points",
        "waypoint_grid_stride",
        "waypoint_pass_nm",
    }
)


def product_params(**overrides: Any) -> RouterParams:
    """Router settings the app uses, plus optional trial overrides."""
    kwargs: dict[str, Any] = {
        "time_step_s": PRODUCT_TIME_STEP_S,
        "heading_step_deg": PRODUCT_HEADING_STEP_DEG,
    }
    kwargs.update(overrides)
    valid = {f.name for f in fields(RouterParams)}
    unknown = set(kwargs) - valid
    if unknown:
        raise KeyError(f"Unknown RouterParams: {sorted(unknown)}")
    return RouterParams(**kwargs)


def params_snapshot(params: RouterParams) -> dict[str, Any]:
    return asdict(params)
