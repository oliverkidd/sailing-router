"""Tuning-schema catalog for the routing lab."""

from __future__ import annotations

from dataclasses import asdict, fields
from datetime import UTC, datetime

from pydantic.fields import FieldInfo

from .router import RouterParams
from .schemas import (
    BoatConfig,
    Course,
    RouteRequest,
    RouterParamsOverride,
    TuningField,
    TuningSchemaResponse,
)

_CATEGORIES = [
    "Resolution",
    "Pruning",
    "Maneuver penalties",
    "Tack / gybe decisions",
    "Commit & rethink",
    "Wind sensitivity",
    "Depth & safety",
    "Near-mark & arrival",
    "Boat",
]

# name -> (category, label, description, step)
_META: dict[str, tuple[str, str, str, float]] = {
    "time_step_s": ("Resolution", "Time step (s)", "Sub-step / commit granularity in seconds.", 10),
    "heading_step_deg": ("Resolution", "Heading step (°)", "Fan / heading discreteness in degrees.", 0.5),
    "sector_width_deg": ("Pruning", "Sector width (°)", "Angular bins used when pruning the frontier.", 0.5),
    "cluster_cell_deg": ("Pruning", "Cluster cell (°)", "Spatial cell size for prune clustering (~deg).", 0.005),
    "max_frontier": ("Pruning", "Max frontier", "Maximum nodes kept after each prune.", 50),
    "max_steps_per_leg": ("Pruning", "Max steps / leg", "Hard expansion-loop cap per leg.", 100),
    "prune_floor_bsp_kn": ("Pruning", "Prune floor BSP (kn)", "BSP floor used in the ETA prune proxy.", 0.05),
    "soft_cost_s_per_nm": ("Pruning", "Soft cost (s / nm)", "Converts nm-equivalent maneuver costs into seconds.", 10),
    "maneuver_prune_nm": ("Maneuver penalties", "Maneuver prune (nm)", "Soft prune cost per tack/gybe, in nm-equivalent.", 0.05),
    "maneuver_overflow_nm": ("Maneuver penalties", "Overflow prune (nm)", "Extra prune cost per maneuver beyond the consecutive cap.", 0.1),
    "max_consecutive_maneuvers": ("Maneuver penalties", "Max consecutive maneuvers", "Soft cap before overflow prune cost applies.", 1),
    "course_change_free_deg": ("Maneuver penalties", "Free course change (°)", "Course changes smaller than this cost nothing.", 0.5),
    "course_change_moderate_deg": ("Maneuver penalties", "Moderate course change (°)", "Upper bound of the moderate-turn band.", 0.5),
    "course_change_tws_scale_kn": ("Maneuver penalties", "Heavy-air TWS scale (kn)", "TWS at which course-change cost starts scaling.", 0.5),
    "course_change_heavy_frac_small": ("Maneuver penalties", "Heavy-air small-turn frac", "Fraction of tack penalty for moderate turns in heavy air.", 0.05),
    "course_change_heavy_frac_large": ("Maneuver penalties", "Heavy-air large-turn frac", "Fraction of tack penalty for large turns in heavy air.", 0.05),
    "course_change_mild_frac": ("Maneuver penalties", "Mild-turn frac", "Default same-tack course-change fraction of tack penalty.", 0.05),
    "same_heading_bonus_s": ("Maneuver penalties", "Same-heading bonus (s)", "Prune bonus for continuing the current heading.", 0.5),
    "same_heading_band_deg": ("Maneuver penalties", "Same-heading band (°)", "Heading delta that still counts as same heading.", 0.1),
    "min_bsp_fraction_of_beat": ("Tack / gybe decisions", "Min BSP fraction of beat", "Reject headings slower than this fraction of beat BSP.", 0.05),
    "vmg_hysteresis_deg": ("Tack / gybe decisions", "VMG hysteresis (°)", "Stay on current tack/gybe unless opposite is this much closer to the mark.", 1),
    "downwind_twa_gate_deg": ("Tack / gybe decisions", "Downwind TWA gate (°)", "TWA at/above which a downwind VMG heading is offered.", 1),
    "heading_ref_max_err_deg": ("Tack / gybe decisions", "Heading-ref max error (°)", "Max course error vs mark before VMG is scored off the seed.", 1),
    "opposite_maneuver_course_err_deg": ("Tack / gybe decisions", "Opposite-maneuver course error (°)", "Course error that unlocks the opposite tack/gybe.", 1),
    "opposite_tack_twa_slack_deg": ("Tack / gybe decisions", "Opposite-tack TWA slack (°)", "How far inside upwind TWA before offering the opposite tack.", 0.5),
    "opposite_gybe_twa_slack_deg": ("Tack / gybe decisions", "Opposite-gybe TWA slack (°)", "How far past downwind TWA before offering the opposite gybe.", 0.5),
    "opposite_maneuver_heading_spread_deg": ("Tack / gybe decisions", "Opposite-maneuver heading spread (°)", "Heading spread vs reference that unlocks the opposite tack/gybe.", 1),
    "max_commit_s": ("Commit & rethink", "Max commit (s)", "Longest time to hold one heading.", 60),
    "no_progress_steps": ("Commit & rethink", "No-progress steps", "Base stall limit before a mark is unreachable.", 5),
    "stall_rem_factor": ("Commit & rethink", "Stall remaining factor", "Adaptive stall limit ≈ rem_nm × factor + offset.", 0.5),
    "stall_rem_offset": ("Commit & rethink", "Stall remaining offset", "Added to the adaptive stall limit.", 1),
    "wind_epoch_commit_frac": ("Commit & rethink", "Wind-epoch commit frac", "Fraction of time_step used as a minimum commit when clipped to the next wind epoch.", 0.05),
    "commit_arrival_buffer_s": ("Commit & rethink", "Commit arrival buffer (s)", "Extra seconds allowed when clipping a commit to remaining distance.", 5),
    "geo_hit_retry_fraction": ("Commit & rethink", "Geo-hit retry fraction", "Retry shorter commits when this fraction of candidates hit land/depth.", 0.05),
    "short_commit_min_s": ("Commit & rethink", "Short commit min (s)", "Minimum duration of adaptive short commits (near-mark / geo retry).", 5),
    "creep_commit_min_s": ("Commit & rethink", "Creep commit min (s)", "Minimum duration of last-ditch creep commits.", 5),
    "rethink_heading_vs_bearing_deg": ("Commit & rethink", "Rethink heading vs bearing (°)", "Do not rethink if already aimed this close to the mark.", 0.5),
    "layline_stop_deg": ("Commit & rethink", "Layline stop (°)", "Stop a commit when past the layline by this much (and can point).", 1),
    "past_layline_deg": ("Commit & rethink", "Past-layline (°)", "Hard stop a commit when heading is this far from mark bearing.", 1),
    "wind_delta_twd_deg": ("Wind sensitivity", "Wind ΔTWD (°)", "Abort a commit when TWD shifts by this much.", 0.5),
    "wind_delta_tws_kn": ("Wind sensitivity", "Wind ΔTWS (kn)", "Abort a commit when TWS shifts by this much.", 0.1),
    "light_air_twd_tws_kn": ("Wind sensitivity", "Light-air TWD ignore (kn)", "Below this TWS, TWD swings are treated as noise.", 0.1),
    "depth_safety_margin_m": ("Depth & safety", "Depth safety margin (m)", "Added to min_depth_m for the hard depth cutoff.", 0.1),
    "mark_probe_nm": ("Near-mark & arrival", "Mark probe (nm)", "Probe length used to test whether the mark bearing is clear.", 0.05),
    "near_mark_nm": ("Near-mark & arrival", "Near-mark (nm)", "Inside this range: shorter commits, no opposite tack/gybe.", 0.05),
    "fan_half_max_deg": ("Near-mark & arrival", "Fan half-max (°)", "Maximum fan half-width from the seed heading.", 5),
    "fan_probe_nm_max": ("Near-mark & arrival", "Fan probe max (nm)", "Upper cap on fan clearance-probe length.", 0.05),
    "fan_probe_nm_min": ("Near-mark & arrival", "Fan probe min (nm)", "Lower cap on fan clearance-probe length.", 0.01),
    "fan_probe_mark_frac": ("Near-mark & arrival", "Fan probe mark frac", "Fan probe length as a fraction of mark_probe_nm.", 0.05),
    "finish_snap_radius_nm": ("Near-mark & arrival", "Finish snap radius (nm)", "Finish is treated as a mark with this radius.", 0.01),
    "mark_arrival_break_nm": ("Near-mark & arrival", "Mark arrival break (nm)", "Early-exit a commit when this close to the target.", 0.005),
}

_BOAT_META: dict[str, tuple[str, str, str, float, float | None, float | None, bool]] = {
    "tack_penalty_s": ("Boat", "Tack penalty (s)", "Time added when tacking.", 1, 0, 300, False),
    "gybe_penalty_s": ("Boat", "Gybe penalty (s)", "Time added when gybing.", 1, 0, 300, False),
    "upwind_twa_deg": ("Boat", "Upwind TWA override (°)", "Override beat angle; empty uses polar VMG.", 0.5, 20, 80, True),
    "downwind_twa_deg": ("Boat", "Downwind TWA override (°)", "Override run angle; empty uses polar VMG.", 0.5, 100, 180, True),
    "leeway_coeff": ("Boat", "Leeway coeff", "Leeway magnitude scale (0 = off).", 0.05, 0, 5, False),
    "leeway_max_deg": ("Boat", "Leeway max (°)", "Cap on leeway angle.", 0.5, 0, 30, False),
    "polar_efficiency": ("Boat", "Polar efficiency", "Multiplier on polar boat speed.", 0.01, 0.5, 1.2, False),
}


def _field_bounds(info: FieldInfo) -> tuple[float | None, float | None]:
    lo = hi = None
    for m in info.metadata:
        ge = getattr(m, "ge", None)
        gt = getattr(m, "gt", None)
        le = getattr(m, "le", None)
        lt = getattr(m, "lt", None)
        if ge is not None:
            lo = float(ge)
        elif gt is not None:
            lo = float(gt)
        if le is not None:
            hi = float(le)
        elif lt is not None:
            hi = float(lt)
    return lo, hi


def build_router_params(req: RouteRequest) -> RouterParams:
    """Merge request-level time/heading steps with optional RouterParamsOverride."""
    kwargs: dict = {
        "time_step_s": req.time_step_s,
        "heading_step_deg": req.heading_step_deg,
    }
    if req.router_params is not None:
        kwargs.update(req.router_params.model_dump(exclude_unset=True))
    valid = {f.name for f in fields(RouterParams)}
    return RouterParams(**{k: v for k, v in kwargs.items() if k in valid})


def _dummy_course() -> Course:
    return Course(
        start=(50.75, -1.3),
        finish=(50.76, -1.29),
        depart_utc=datetime(1970, 1, 1, tzinfo=UTC),
    )


def tuning_schema() -> TuningSchemaResponse:
    defaults = asdict(RouterParams())
    override_fields = RouterParamsOverride.model_fields
    boat_defaults = BoatConfig(polar_id="").model_dump()
    course_defaults = _dummy_course().model_dump()
    req_defaults = RouteRequest(
        course=_dummy_course(),
        boat=BoatConfig(polar_id=""),
        model_ids=["gfs"],
    ).model_dump()

    out: list[TuningField] = []
    for f in fields(RouterParams):
        name = f.name
        cat, label, desc, step = _META.get(
            name,
            ("Pruning", name.replace("_", " "), name, 1.0),
        )
        info = override_fields.get(name)
        lo, hi = _field_bounds(info) if info is not None else (None, None)
        group = "request" if name in ("time_step_s", "heading_step_deg") else "router"
        default = req_defaults[name] if group == "request" else defaults.get(name)
        out.append(
            TuningField(
                name=name,
                category=cat,
                label=label,
                description=desc,
                default=default,
                min=lo,
                max=hi,
                step=step,
                group=group,
            )
        )

    for name, (cat, label, desc, step, lo, hi, nullable) in _BOAT_META.items():
        out.append(
            TuningField(
                name=name,
                category=cat,
                label=label,
                description=desc,
                default=boat_defaults.get(name),
                min=lo,
                max=hi,
                step=step,
                group="boat",
                nullable=nullable,
            )
        )

    out.append(
        TuningField(
            name="min_depth_m",
            category="Depth & safety",
            label="Min depth (m)",
            description="Hard depth floor before the safety margin is added.",
            default=course_defaults.get("min_depth_m", 3.0),
            min=0,
            max=30,
            step=0.1,
            group="course",
        )
    )

    cats = [c for c in _CATEGORIES if any(f.category == c for f in out)]
    return TuningSchemaResponse(categories=cats, fields=out, router_defaults=defaults)
