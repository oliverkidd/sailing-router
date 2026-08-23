#!/usr/bin/env python3
"""Offline deterministic routing validation harness.

Runs IsochroneRouter against SyntheticWindField + Solent sample courses so
quality gates never depend on live GRIB/WCS. Captures baseline.json for
regression comparison across the routing accuracy overhaul.

Usage (from services/api):
  .venv/bin/python scripts/validate_routing.py --sample
  .venv/bin/python scripts/validate_routing.py --sample --gate 0
  .venv/bin/python scripts/validate_routing.py --sample --gate 1
  .venv/bin/python scripts/validate_routing.py --sample --gate 2
  .venv/bin/python scripts/validate_routing.py --sample --acceptance
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from region import SAMPLE_BBOX  # noqa: E402

from app import store  # noqa: E402
from app.boat import Boat  # noqa: E402
from app.geo import (  # noqa: E402
    DepthField,
    GeoContext,
    LandMask,
    bearing,
    course_bbox,
    distance_nm,
)
from app.geo.fetch import ensure_geo_context  # noqa: E402
from app.navmath import signed_diff, twa_from  # noqa: E402
from app.router import IsochroneRouter, Node, RouterParams  # noqa: E402
from app.schemas import BoatConfig, Course, LatLon, Mark, ModelRoute  # noqa: E402

BASELINE_DIR = ROOT / "data" / "cache" / "routing_baseline"
BASELINE_PATH = BASELINE_DIR / "baseline.json"
BUGGY_BASELINE_PATH = BASELINE_DIR / "buggy_layline_baseline.json"
DEPART = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)

# Constant wind used by reach / upwind / cove / layline / pinch (TWD = from).
WIND_TWS_KN = 12.0
WIND_TWD_DEG = 270.0  # from the west


@dataclass
class SyntheticWindField:
    """Constant (or optionally spatially varying) wind matching WindField.wind_at."""

    tws: float = WIND_TWS_KN
    twd: float = WIND_TWD_DEG
    # Optional: stronger wind east of lon_boost_east_of (knots).
    boost_tws: float | None = None
    lon_boost_east_of: float | None = None

    def wind_at(self, lat: float, lon: float, t: datetime) -> tuple[float, float] | None:
        del lat, t
        tws = self.tws
        if (
            self.boost_tws is not None
            and self.lon_boost_east_of is not None
            and lon >= self.lon_boost_east_of
        ):
            tws = self.boost_tws
        return tws, self.twd


@dataclass
class Scenario:
    name: str
    start: LatLon
    finish: LatLon
    min_depth_m: float = 3.0
    description: str = ""
    wind_twd: float | None = None
    wind_tws: float | None = None
    # When True, use synthetic shoal-with-gap geo instead of chart tiles.
    synthetic_gap: bool = False
    # When True, use synthetic narrow channel geo (turnaround stress test).
    synthetic_channel: bool = False
    marks: list[LatLon] = field(default_factory=list)


# Solent SAMPLE_BBOX: lon -1.75..-0.95, lat 50.55..50.95
# Default wind from west (270): westbound ≈ dead upwind; eastbound ≈ dead downwind.
# Reach uses wind from north so eastbound is a true beam reach (TWA ≈ 90).
# Coordinates stay in the deep mid-Solent corridor (chart depth >= ~5 m).
SCENARIOS: dict[str, Scenario] = {
    "reach": Scenario(
        name="reach",
        # Mid-Solent channel, eastbound beam reach in northerly (~3 nm).
        start=(50.76, -1.42),
        finish=(50.76, -1.34),
        wind_twd=0.0,  # from north → TWA ≈ 90° when sailing east
        description="Beam reach eastbound in northerly — expect 0 tacks",
    ),
    "upwind": Scenario(
        name="upwind",
        # Mid-Solent channel, westbound → dead upwind in westerly (~3 nm).
        start=(50.76, -1.34),
        finish=(50.76, -1.42),
        description="Dead upwind westbound in westerly — expect few long tacks",
    ),
    "cove": Scenario(
        name="cove",
        # Start toward Cowes approaches, finish west in open channel.
        start=(50.77, -1.30),
        finish=(50.76, -1.40),
        description="Start near Cowes shoreline — stress test for trapping",
    ),
    "layline": Scenario(
        name="layline",
        # Mid-channel upwind with a north offset so the router is tempted to
        # overstand the starboard layline before reaching back to the finish.
        start=(50.762, -1.34),
        finish=(50.75, -1.42),
        description="Upwind with offset start — expect no layline overshoot",
    ),
    "pinch": Scenario(
        name="pinch",
        # Dead upwind finish so raw bearing-to-mark sits in the no-go zone.
        start=(50.76, -1.36),
        finish=(50.76, -1.42),
        description="Dead-upwind approach — expect no unsailable mark bearing",
    ),
    "gap": Scenario(
        name="gap",
        # Synthetic shoal barrier with a southern gap; wind from NE.
        # Optimal: bear away through gap then head up to the mark.
        start=(50.780, -1.30),
        finish=(50.760, -1.38),
        wind_twd=45.0,
        wind_tws=12.0,
        synthetic_gap=True,
        min_depth_m=3.0,
        description="Shoal with gap — expect clean bear-away through gap, no thrash",
    ),
    "channel": Scenario(
        name="channel",
        # Enter a narrow N-S corridor to a mark, then turn around to finish south.
        start=(50.752, -1.340),
        marks=[(50.778, -1.340)],
        finish=(50.750, -1.355),
        wind_twd=270.0,
        wind_tws=12.0,
        synthetic_channel=True,
        min_depth_m=3.0,
        description="Narrow channel mark then turnaround to finish",
    ),
}


@dataclass
class ScenarioMetrics:
    name: str
    elapsed_s: int
    distance_nm: float
    n_tacks: int
    n_gybes: int
    forecast_exhausted: bool
    n_legs: int
    min_depth_m_along: float | None
    any_on_land: bool
    any_below_min_depth: bool
    max_consecutive_maneuvers: int
    max_cross_track_nm: float
    recomputed_tacks: int
    min_tack_leg_nm: float | None
    unreachable: bool = False
    warnings: list[str] = field(default_factory=list)
    positions: list[LatLon] = field(default_factory=list)
    min_sog: float | None = None
    overshoot_nm: float = 0.0
    remaining_monotonic: bool = True
    mean_heading_on_layline_deg: float | None = None
    max_midleg_heading_delta_deg: float = 0.0


def _max_midleg_heading_delta_deg(route: ModelRoute) -> float:
    """Max |Δheading| between successive sailing legs, excluding the first step.

    Captures open-water wiggles after the initial course is set. Mark snaps,
    the start node (tws=0), and the very first heading choice are ignored.
    """
    best = 0.0
    prev: float | None = None
    saw_first = False
    for leg in route.legs:
        if leg.heading is None or leg.maneuver == "mark":
            continue
        # Start node is serialised with heading 0 / tws 0 — not a sailing leg.
        if (leg.tws or 0.0) <= 1e-6 and (leg.bsp or 0.0) <= 1e-6:
            continue
        if not saw_first:
            prev = float(leg.heading)
            saw_first = True
            continue
        if prev is not None:
            best = max(best, abs(signed_diff(float(leg.heading), prev)))
        prev = float(leg.heading)
    return best


def _cross_track_nm(start: LatLon, finish: LatLon, point: LatLon) -> float:
    """Approximate cross-track distance from the great-circle rhumb corridor."""
    brg_route = bearing(start, finish)
    brg_pt = bearing(start, point)
    dist = distance_nm(start, point)
    angle = abs(signed_diff(brg_pt, brg_route))
    return abs(dist * math.sin(math.radians(angle)))


def _overshoot_nm(positions: list[LatLon], finish: LatLon) -> float:
    """How far the boat sailed away from the mark after having gotten closer.

    Captures "overstood the layline then reached back": remaining distance
    increases after a prior closer approach.
    """
    if not positions:
        return 0.0
    best = float("inf")
    overshoot = 0.0
    for pos in positions:
        rem = distance_nm(pos, finish)
        if rem < best:
            best = rem
        else:
            overshoot = max(overshoot, rem - best)
    return overshoot


def _remaining_monotonic(positions: list[LatLon], finish: LatLon, *, tol_nm: float = 0.15) -> bool:
    """True if remaining-to-finish never increases by more than tol_nm after a step."""
    if len(positions) < 2:
        return True
    prev = distance_nm(positions[0], finish)
    for pos in positions[1:]:
        rem = distance_nm(pos, finish)
        if rem > prev + tol_nm:
            return False
        prev = rem
    return True


def _recompute_tacks(route: ModelRoute) -> tuple[int, float | None]:
    """Independent tack count from successive TWA wind-side flips (upwind)."""
    n = 0
    prev_side: float | None = None
    tack_leg_lengths: list[float] = []
    last_tack_i = 0
    for i, leg in enumerate(route.legs):
        if leg.heading is None:
            continue
        d = signed_diff(leg.twd, leg.heading)
        if abs(d) < 1e-6 or abs(abs(d) - 180.0) < 1e-6:
            side = 0.0
        else:
            side = 1.0 if d > 0 else -1.0
        if prev_side is not None and side != 0.0 and prev_side != 0.0 and side != prev_side:
            if leg.twa < 90.0:
                n += 1
                if i > last_tack_i:
                    seg = 0.0
                    for j in range(last_tack_i + 1, i + 1):
                        seg += distance_nm(route.legs[j - 1].position, route.legs[j].position)
                    tack_leg_lengths.append(seg)
                last_tack_i = i
        if side != 0.0:
            prev_side = side
    min_leg = min(tack_leg_lengths) if tack_leg_lengths else None
    return n, min_leg


def _max_consecutive_maneuvers(route: ModelRoute) -> int:
    best = 0
    run = 0
    for leg in route.legs:
        if leg.maneuver in ("tack", "gybe"):
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def _build_gap_geo(course: Course) -> GeoContext:
    """Synthetic depth field: deep water with a shallow E-W barrier and a southern gap.

    Barrier spans lon [-1.36, -1.32] at lat ~50.770, with a gap south of lat 50.765
    so the optimal path bears away (south) through the gap then heads up to finish.
    """
    lat_min, lat_max = 50.74, 50.79
    lon_min, lon_max = -1.40, -1.28
    n_lat, n_lon = 80, 80
    lats = np.linspace(lat_min, lat_max, n_lat)
    lons = np.linspace(lon_min, lon_max, n_lon)
    depth = np.full((n_lat, n_lon), 20.0, dtype=np.float64)
    for i, la in enumerate(lats):
        for j, lo in enumerate(lons):
            # Shallow barrier band between start and finish, gap to the south.
            if -1.355 <= lo <= -1.325 and la >= 50.765:
                depth[i, j] = 1.0  # below min_depth → blocked
    land = LandMask([])
    return GeoContext.build(
        land=land,
        depth=DepthField(lats, lons, depth),
        exclusion_rings=[],
        min_depth_m=course.min_depth_m,
    )


def _build_channel_geo(course: Course) -> GeoContext:
    """Synthetic N-S navigable corridor (~0.2 nm wide) with shallow banks.

    Forces the router to enter the channel to a northern mark, then turn around
    and exit south to the finish — the failure mode that emptied the frontier
    when the approach heading stayed locked.
    """
    lat_min, lat_max = 50.745, 50.785
    lon_min, lon_max = -1.370, -1.310
    n_lat, n_lon = 100, 80
    lats = np.linspace(lat_min, lat_max, n_lat)
    lons = np.linspace(lon_min, lon_max, n_lon)
    depth = np.full((n_lat, n_lon), 1.0, dtype=np.float64)  # shallow default
    for i, la in enumerate(lats):
        for j, lo in enumerate(lons):
            # Narrow deep corridor centered on lon -1.340
            if abs(lo - (-1.340)) <= 0.004:
                depth[i, j] = 20.0
            # Open water south of the channel mouth
            if la <= 50.754 and -1.360 <= lo <= -1.320:
                depth[i, j] = 20.0
    land = LandMask([])
    return GeoContext.build(
        land=land,
        depth=DepthField(lats, lons, depth),
        exclusion_rings=[],
        min_depth_m=course.min_depth_m,
    )


def _build_geo(course: Course) -> GeoContext:
    """Real GeoContext for the course; fall back to empty land + deep water if fetch fails."""
    try:
        return ensure_geo_context(course, margin_deg=0.5)
    except Exception as exc:  # noqa: BLE001
        print(f"[validate] ensure_geo_context failed ({exc}); using local land + deep depth")
        from app.geo.fetch import fetch_land_for_bbox

        bbox = course_bbox(course, margin_deg=0.5)
        land = fetch_land_for_bbox(bbox, require_hires=False)
        # Flat deep water so Phase 0 can still measure algorithm behaviour offline.
        depth = DepthField(
            lats=np.array([bbox.lat_min, bbox.lat_max]),
            lons=np.array([bbox.lon_min, bbox.lon_max]),
            depth_m=np.array([[100.0, 100.0], [100.0, 100.0]]),
        )
        return GeoContext.build(
            land=land,
            depth=depth,
            exclusion_rings=[],
            min_depth_m=course.min_depth_m,
        )


def run_scenario(
    scenario: Scenario,
    *,
    boat: Boat,
    geo: GeoContext | None = None,
    wind: SyntheticWindField | None = None,
    params: RouterParams | None = None,
    tack_penalty_s: float | None = None,
    boat_overrides: dict[str, Any] | None = None,
) -> ScenarioMetrics:
    cfg = boat.config
    if tack_penalty_s is not None or boat_overrides:
        overrides = dict(boat_overrides or {})
        if tack_penalty_s is not None:
            overrides["tack_penalty_s"] = tack_penalty_s
        cfg = BoatConfig(
            polar_id=cfg.polar_id,
            tack_penalty_s=overrides.get("tack_penalty_s", cfg.tack_penalty_s),
            gybe_penalty_s=overrides.get("gybe_penalty_s", cfg.gybe_penalty_s),
            upwind_twa_deg=overrides.get("upwind_twa_deg", cfg.upwind_twa_deg),
            downwind_twa_deg=overrides.get("downwind_twa_deg", cfg.downwind_twa_deg),
            leeway_coeff=overrides.get("leeway_coeff", cfg.leeway_coeff),
            leeway_max_deg=overrides.get("leeway_max_deg", cfg.leeway_max_deg),
            polar_efficiency=overrides.get("polar_efficiency", cfg.polar_efficiency),
        )
        boat = Boat(cfg, boat.polar)

    course = Course(
        start=scenario.start,
        finish=scenario.finish,
        marks=[
            Mark(position=m, radius_nm=0.1, name=f"M{i + 1}")
            for i, m in enumerate(scenario.marks)
        ],
        exclusions=[],
        depart_utc=DEPART,
        min_depth_m=scenario.min_depth_m,
    )
    if geo is None:
        if scenario.synthetic_channel:
            geo = _build_channel_geo(course)
        elif scenario.synthetic_gap:
            geo = _build_gap_geo(course)
        else:
            geo = _build_geo(course)
    # Rebuild geo with scenario min_depth if needed.
    if geo.min_depth_m != scenario.min_depth_m:
        geo = GeoContext.build(
            land=geo.land,
            depth=geo.depth,
            exclusion_rings=[],
            min_depth_m=scenario.min_depth_m,
        )

    if wind is None:
        wind = SyntheticWindField(
            tws=scenario.wind_tws if scenario.wind_tws is not None else WIND_TWS_KN,
            twd=scenario.wind_twd if scenario.wind_twd is not None else WIND_TWD_DEG,
        )
    params = params or RouterParams()
    engine = IsochroneRouter(boat=boat, wind=wind, geo=geo, params=params)
    route = engine.route(course, model_id="synthetic")

    warnings: list[str] = []
    unreachable = bool(route.unreachable)
    if route.warnings:
        warnings.extend(route.warnings)
    if route.forecast_exhausted:
        warnings.append("forecast_exhausted")
    # Partial route that never reached finish radius.
    if route.legs:
        last = route.legs[-1].position
        if distance_nm(last, scenario.finish) > 0.25:
            unreachable = True
            if "finish_not_reached" not in warnings:
                warnings.append("finish_not_reached")

    min_depth: float | None = None
    any_on_land = False
    any_below = False
    max_xt = 0.0
    positions: list[LatLon] = []
    sogs: list[float] = []
    for leg in route.legs:
        pos = leg.position
        positions.append(pos)
        max_xt = max(max_xt, _cross_track_nm(scenario.start, scenario.finish, pos))
        if geo.land.on_land(pos):
            any_on_land = True
        # Depth invariant vs chart min_depth (not safety margin).
        d = None
        if leg.depth_m is not None:
            d = leg.depth_m
        elif geo.depth is not None:
            d = geo.depth.depth(pos)
        if d is not None:
            if min_depth is None or d < min_depth:
                min_depth = d
            if d < scenario.min_depth_m:
                any_below = True
        # Skip start node (sog=0) and near-zero numerical noise.
        if leg.sog is not None and leg.heading is not None and leg.sog > 1e-6:
            sogs.append(float(leg.sog))

    recomputed, min_tack_leg = _recompute_tacks(route)
    min_sog = min(sogs) if sogs else None
    overshoot = _overshoot_nm(positions, scenario.finish)
    mono = _remaining_monotonic(positions, scenario.finish)

    # Mean heading of legs that look like the boat's upwind layline (TWA near target).
    up_twa = boat.target_upwind_twa(wind.tws)
    lay_headings: list[float] = []
    for leg in route.legs:
        if leg.heading is None or leg.twa is None:
            continue
        if abs(leg.twa - up_twa) < 8.0:
            lay_headings.append(float(leg.heading))
    mean_lay = sum(lay_headings) / len(lay_headings) if lay_headings else None
    midleg_delta = _max_midleg_heading_delta_deg(route)

    return ScenarioMetrics(
        name=scenario.name,
        elapsed_s=route.elapsed_s,
        distance_nm=route.distance_nm,
        n_tacks=route.n_tacks,
        n_gybes=route.n_gybes,
        forecast_exhausted=route.forecast_exhausted,
        n_legs=len(route.legs),
        min_depth_m_along=None if min_depth is None else round(min_depth, 2),
        any_on_land=any_on_land,
        any_below_min_depth=any_below,
        max_consecutive_maneuvers=_max_consecutive_maneuvers(route),
        max_cross_track_nm=round(max_xt, 3),
        recomputed_tacks=recomputed,
        min_tack_leg_nm=None if min_tack_leg is None else round(min_tack_leg, 3),
        unreachable=unreachable,
        warnings=warnings,
        positions=positions,
        min_sog=None if min_sog is None else round(min_sog, 3),
        overshoot_nm=round(overshoot, 3),
        remaining_monotonic=mono,
        mean_heading_on_layline_deg=None if mean_lay is None else round(mean_lay, 1),
        max_midleg_heading_delta_deg=round(midleg_delta, 1),
    )


def _offers_unsailable_mark_bearing(
    boat: Boat,
    *,
    tws: float = WIND_TWS_KN,
    twd: float = WIND_TWD_DEG,
) -> bool:
    """True if _candidate_headings offers a raw mark bearing inside the no-go zone.

    Reproduces the pinch bug: boat is dead upwind of the mark, so bearing-to-mark
    has TWA ≈ 0 and must NOT be offered as a candidate after the Phase 2 fix.
    """
    # Node east of mark; mark is due west (= into the westerly).
    node = Node(lat=50.76, lon=-1.36, t=DEPART, heading=320.0, twd=twd, tws=tws)
    target = (50.76, -1.42)
    brg = bearing(node.pos, target)
    twa_brg = twa_from(brg, twd)
    up_twa = boat.target_upwind_twa(tws)
    # Sanity: mark bearing is unsailable.
    if twa_brg >= up_twa - 1.0:
        return False
    # Minimal geo so IsochroneRouter can construct (no hazard needed).
    depth = DepthField(
        lats=np.array([50.7, 50.8]),
        lons=np.array([-1.5, -1.2]),
        depth_m=np.array([[20.0, 20.0], [20.0, 20.0]]),
    )
    geo = GeoContext.build(
        land=LandMask([]),
        depth=depth,
        exclusion_rings=[],
        min_depth_m=3.0,
    )
    engine = IsochroneRouter(
        boat=boat,
        wind=SyntheticWindField(tws=tws, twd=twd),
        geo=geo,
        params=RouterParams(),
    )
    heads = engine._candidate_headings(node, tws, twd, target)
    # Match if any candidate is within 2° of the raw (unsailable) mark bearing.
    return any(abs(signed_diff(h, brg)) < 2.0 for h in heads)


def _metrics_public(m: ScenarioMetrics) -> dict[str, Any]:
    d = asdict(m)
    # Keep baseline compact — drop full track unless needed.
    d.pop("positions", None)
    return d


def load_baseline() -> dict[str, Any] | None:
    if not BASELINE_PATH.exists():
        return None
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def save_baseline(results: dict[str, ScenarioMetrics], path: Path = BASELINE_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "captured_utc": datetime.now(UTC).isoformat(),
        "wind": {"tws": WIND_TWS_KN, "twd": WIND_TWD_DEG},
        "sample_bbox": SAMPLE_BBOX,
        "scenarios": {k: _metrics_public(v) for k, v in results.items()},
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _make_boat(*, overrides: dict[str, Any] | None = None) -> Boat:
    polar_id = store.ensure_sample()
    polar = store.get_polar(polar_id)
    kwargs: dict[str, Any] = {"polar_id": polar_id}
    if overrides:
        kwargs.update(overrides)
    cfg = BoatConfig(**kwargs)
    return Boat(cfg, polar)


def run_all(
    names: list[str] | None = None,
    *,
    tack_penalty_s: float | None = None,
    shared_geo: GeoContext | None = None,
) -> dict[str, ScenarioMetrics]:
    boat = _make_boat()
    out: dict[str, ScenarioMetrics] = {}
    selected = names or list(SCENARIOS.keys())
    for name in selected:
        sc = SCENARIOS[name]
        print(f"[validate] running {name}: {sc.description}")
        # Per-scenario geo (tighter bbox → higher-res depth) unless caller shares one.
        geo = shared_geo
        if geo is None and not sc.synthetic_gap and not sc.synthetic_channel:
            course = Course(
                start=sc.start,
                finish=sc.finish,
                depart_utc=DEPART,
                min_depth_m=sc.min_depth_m,
            )
            geo = _build_geo(course)
        m = run_scenario(sc, boat=boat, geo=geo, tack_penalty_s=tack_penalty_s)
        out[name] = m
        print(
            f"  elapsed={m.elapsed_s}s dist={m.distance_nm}nm "
            f"tacks={m.n_tacks} gybes={m.n_gybes} xt={m.max_cross_track_nm}nm "
            f"min_depth={m.min_depth_m_along} unreachable={m.unreachable} "
            f"consec_man={m.max_consecutive_maneuvers} "
            f"min_sog={m.min_sog} overshoot={m.overshoot_nm}nm "
            f"midΔh={m.max_midleg_heading_delta_deg}° mono={m.remaining_monotonic}"
        )
    return out


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


class GateFailure(Exception):
    pass


def _assert(cond: bool, msg: str, failures: list[str]) -> None:
    if not cond:
        failures.append(msg)
        print(f"  FAIL: {msg}")
    else:
        print(f"  ok: {msg}")


def gate0(results: dict[str, ScenarioMetrics]) -> None:
    failures: list[str] = []
    _assert(set(results) >= {"reach", "upwind", "cove"}, "all three scenarios present", failures)
    _assert(BASELINE_PATH.exists(), f"baseline written at {BASELINE_PATH}", failures)
    for name, m in results.items():
        _assert(m.n_legs > 0, f"{name}: produced legs", failures)
    # Reach/upwind must actually finish so later gates have a real baseline ETA.
    _assert(not results["reach"].unreachable, "reach: finished (baseline usable)", failures)
    _assert(not results["upwind"].unreachable, "upwind: finished (baseline usable)", failures)
    if failures:
        raise GateFailure(f"GATE 0 failed ({len(failures)}): " + "; ".join(failures))
    print("[GATE 0] PASS")


def gate1(results: dict[str, ScenarioMetrics]) -> None:
    """Phase 1: new metrics present; buggy behaviours are observed (baseline capture).

    GATE 1 documents the pre-fix failures so later phases can flip them green.
    It PASSES when the harness successfully reproduces the known bugs (or, after
    fixes land, when the new scenarios simply report the metrics cleanly and the
    buggy baseline file exists from the capture run).
    """
    failures: list[str] = []
    for name in ("layline", "pinch", "gap"):
        _assert(name in results, f"{name} scenario present", failures)
        m = results[name]
        _assert(m.n_legs > 0, f"{name}: produced legs", failures)
        if not m.unreachable:
            _assert(m.min_sog is not None, f"{name}: min_sog metric present", failures)

    pinch = results["pinch"]
    lay = results["layline"]
    gap = results["gap"]
    print(
        f"  [diag] pinch min_sog={pinch.min_sog} "
        f"layline overshoot={lay.overshoot_nm}nm unreachable={lay.unreachable} "
        f"gap tacks={gap.n_tacks} gybes={gap.n_gybes} "
        f"consec={gap.max_consecutive_maneuvers} mono={gap.remaining_monotonic}"
    )
    # Pre-fix: candidate headings still offer the unsailable mark bearing.
    boat = _make_boat()
    offers = _offers_unsailable_mark_bearing(boat)
    print(f"  [diag] offers_unsailable_mark_bearing={offers}")

    # Capability check: BoatConfig overrides must change layline headings.
    boat_lo = _make_boat(overrides={"upwind_twa_deg": 35.0})
    boat_hi = _make_boat(overrides={"upwind_twa_deg": 50.0})
    sc = SCENARIOS["layline"]
    course = Course(
        start=sc.start,
        finish=sc.finish,
        depart_utc=DEPART,
        min_depth_m=sc.min_depth_m,
    )
    geo = _build_geo(course)
    m_lo = run_scenario(sc, boat=boat_lo, geo=geo)
    m_hi = run_scenario(sc, boat=boat_hi, geo=geo)
    # Target angles from BoatConfig must differ; headings on the beat should shift.
    tws = WIND_TWS_KN
    ang_lo = boat_lo.target_upwind_twa(tws)
    ang_hi = boat_hi.target_upwind_twa(tws)
    _assert(ang_lo == 35.0, f"capability: upwind_twa override 35 (got {ang_lo})", failures)
    _assert(ang_hi == 50.0, f"capability: upwind_twa override 50 (got {ang_hi})", failures)
    print(
        f"  [capability] twa35 mean_lay={m_lo.mean_heading_on_layline_deg} "
        f"tacks={m_lo.n_tacks} elapsed={m_lo.elapsed_s} | "
        f"twa50 mean_lay={m_hi.mean_heading_on_layline_deg} "
        f"tacks={m_hi.n_tacks} elapsed={m_hi.elapsed_s}"
    )
    # Polar-efficiency override must slow the boat (longer ETA).
    boat_slow = _make_boat(overrides={"polar_efficiency": 0.6})
    m_slow = run_scenario(SCENARIOS["reach"], boat=boat_slow)
    m_fast = results.get("reach") or run_scenario(SCENARIOS["reach"], boat=_make_boat())
    if not m_slow.unreachable and not m_fast.unreachable:
        _assert(
            m_slow.elapsed_s > m_fast.elapsed_s,
            f"capability: polar_efficiency 0.6 slower "
            f"({m_slow.elapsed_s}s > {m_fast.elapsed_s}s)",
            failures,
        )
    _assert(
        BUGGY_BASELINE_PATH.exists(),
        f"buggy baseline written ({BUGGY_BASELINE_PATH})",
        failures,
    )
    if failures:
        raise GateFailure(f"GATE 1 failed ({len(failures)}): " + "; ".join(failures))
    print("[GATE 1] PASS (metrics + capability wiring)")


def gate2(results: dict[str, ScenarioMetrics]) -> None:
    failures: list[str] = []
    baseline = load_baseline()
    reach = results["reach"]
    upwind = results["upwind"]
    _assert(reach.n_tacks == 0, f"reach n_tacks==0 (got {reach.n_tacks})", failures)
    _assert(
        reach.max_cross_track_nm <= 0.35,
        f"reach max cross-track <= 0.35 nm (got {reach.max_cross_track_nm})",
        failures,
    )
    _assert(
        reach.max_midleg_heading_delta_deg <= 10.0,
        f"reach mid-leg heading delta <= 10° (got {reach.max_midleg_heading_delta_deg})",
        failures,
    )
    _assert(upwind.n_tacks <= 3, f"upwind n_tacks<=3 (got {upwind.n_tacks})", failures)
    if upwind.n_tacks >= 1:
        _assert(
            upwind.min_tack_leg_nm is None or upwind.min_tack_leg_nm > 0.4,
            f"upwind min tack leg > 0.4 nm (got {upwind.min_tack_leg_nm})",
            failures,
        )
    # Open-water reach/upwind must not thrash with consecutive micro-maneuvers.
    for name in ("reach", "upwind"):
        if name not in results:
            continue
        m = results[name]
        _assert(
            m.max_consecutive_maneuvers <= 3,
            f"{name}: max_consecutive_maneuvers<=3 (got {m.max_consecutive_maneuvers})",
            failures,
        )
    # Pinch: never offer unsailable mark bearing; no sub-1kt crawling legs.
    boat = _make_boat()
    _assert(
        not _offers_unsailable_mark_bearing(boat),
        "pinch: candidate headings omit unsailable mark bearing",
        failures,
    )
    if "pinch" in results and results["pinch"].min_sog is not None:
        _assert(
            results["pinch"].min_sog >= 1.0,
            f"pinch min_sog>=1.0 (got {results['pinch'].min_sog})",
            failures,
        )
    if baseline:
        for name, m in results.items():
            b = baseline["scenarios"].get(name)
            if not b:
                continue
            _assert(
                m.elapsed_s <= b["elapsed_s"] + 300,
                f"{name}: elapsed {m.elapsed_s}s <= baseline {b['elapsed_s']}s (+300s = 1 step)",
                failures,
            )
    if failures:
        raise GateFailure(f"GATE 2 failed ({len(failures)}): " + "; ".join(failures))
    print("[GATE 2] PASS")


def gate3(results: dict[str, ScenarioMetrics], mono: list[tuple[float, ScenarioMetrics]]) -> None:
    failures: list[str] = []
    for name, m in results.items():
        _assert(
            abs(m.n_tacks - m.recomputed_tacks) <= 1,
            f"{name}: n_tacks ({m.n_tacks}) ≈ recomputed ({m.recomputed_tacks})",
            failures,
        )
    # Monotonicity: increasing tack penalty → non-increasing tack count.
    for i in range(1, len(mono)):
        prev_p, prev_m = mono[i - 1]
        pen, m = mono[i]
        _assert(
            m.n_tacks <= prev_m.n_tacks,
            f"penalty {pen}s tacks {m.n_tacks} <= penalty {prev_p}s tacks {prev_m.n_tacks}",
            failures,
        )
    # Layline overshoot must be near zero after Phase 3.
    if "layline" in results:
        lay = results["layline"]
        _assert(
            lay.overshoot_nm <= 0.2,
            f"layline overshoot_nm<=0.2 (got {lay.overshoot_nm})",
            failures,
        )
        _assert(not lay.unreachable, "layline finished", failures)
    if "reach" in results:
        _assert(results["reach"].n_tacks == 0, "reach still 0 tacks", failures)
    if "upwind" in results:
        _assert(
            results["upwind"].n_tacks <= 3,
            f"upwind n_tacks<=3 (got {results['upwind'].n_tacks})",
            failures,
        )
    if failures:
        raise GateFailure(f"GATE 3 failed ({len(failures)}): " + "; ".join(failures))
    print("[GATE 3] PASS")


def gate4(results: dict[str, ScenarioMetrics], phase3_etas: dict[str, int] | None = None) -> None:
    failures: list[str] = []
    for name, m in results.items():
        if m.unreachable:
            continue
        _assert(
            not m.any_below_min_depth,
            f"{name}: no point below min_depth (min along={m.min_depth_m_along})",
            failures,
        )
        _assert(not m.any_on_land, f"{name}: no point on land", failures)
    cove = results.get("cove")
    if cove is not None:
        thrash = cove.max_consecutive_maneuvers > 8 or (
            cove.n_tacks > 15 and cove.distance_nm < 2.0
        )
        _assert(
            cove.unreachable or not thrash,
            f"cove escapes or unreachable (consec={cove.max_consecutive_maneuvers}, "
            f"tacks={cove.n_tacks}, unreachable={cove.unreachable})",
            failures,
        )
    if "gap" in results:
        gap = results["gap"]
        _assert(not gap.unreachable, "gap: finished", failures)
        _assert(
            gap.remaining_monotonic or gap.overshoot_nm <= 0.55,
            f"gap: monotonic progress (mono={gap.remaining_monotonic}, "
            f"overshoot={gap.overshoot_nm})",
            failures,
        )
        thrash_gap = gap.max_consecutive_maneuvers > 4 or (gap.n_tacks + gap.n_gybes) > 6
        _assert(
            not thrash_gap,
            f"gap: no thrash (tacks={gap.n_tacks} gybes={gap.n_gybes} "
            f"consec={gap.max_consecutive_maneuvers})",
            failures,
        )
        _assert(
            not gap.any_below_min_depth,
            f"gap: no depth violation (min={gap.min_depth_m_along})",
            failures,
        )
        # After clearing the shoal, do not keep a long escape heading (xt stays bounded).
        _assert(
            gap.max_cross_track_nm <= 1.2,
            f"gap: post-corridor cross-track <= 1.2 nm (got {gap.max_cross_track_nm})",
            failures,
        )
    if "channel" in results:
        ch = results["channel"]
        _assert(not ch.unreachable, "channel: finished turnaround", failures)
        _assert(
            "frontier empty" not in " ".join(ch.warnings).lower(),
            f"channel: no frontier_empty ({ch.warnings})",
            failures,
        )
    if phase3_etas:
        for name in ("reach", "upwind"):
            if name not in results or name not in phase3_etas:
                continue
            m = results[name]
            ref = phase3_etas[name]
            if ref <= 0:
                continue
            ratio = m.elapsed_s / ref
            _assert(
                ratio <= 1.05,
                f"{name}: ETA within 5% of phase3 ({m.elapsed_s} vs {ref})",
                failures,
            )
    if failures:
        raise GateFailure(f"GATE 4 failed ({len(failures)}): " + "; ".join(failures))
    print("[GATE 4] PASS")


def gate5(results: dict[str, ScenarioMetrics]) -> None:
    """Full acceptance: combine gate2 + gate3 layline + gate4 depth/gap + telemetry."""
    gate2(results)
    gate3(results, mono=[])
    gate4(results)
    # Telemetry: at least one leg should carry depth from the router.
    boat = _make_boat()
    sc = SCENARIOS["reach"]
    course = Course(
        start=sc.start,
        finish=sc.finish,
        depart_utc=DEPART,
        min_depth_m=sc.min_depth_m,
    )
    geo = _build_geo(course)
    route = IsochroneRouter(
        boat=boat, wind=SyntheticWindField(), geo=geo, params=RouterParams()
    ).route(course, "synthetic")
    failures: list[str] = []
    _assert(bool(route.data_source.get("depth")), "data_source.depth present", failures)
    _assert(bool(route.data_source.get("land")), "data_source.land present", failures)
    depths = [leg.depth_m for leg in route.legs if leg.depth_m is not None]
    _assert(len(depths) > 0, "leg depth_m telemetry present", failures)
    # No pinching anywhere across finished scenarios.
    for name, m in results.items():
        if m.unreachable or m.min_sog is None:
            continue
        _assert(m.min_sog >= 1.0, f"{name}: min_sog>=1.0 (got {m.min_sog})", failures)
    # Reach: with constant wind, mid-route SOG should stay near beat/reach speed.
    if "reach" in results and not results["reach"].unreachable:
        reach = results["reach"]
        if reach.min_sog is not None:
            boat = _make_boat()
            beat = boat.bsp(WIND_TWS_KN, boat.target_upwind_twa(WIND_TWS_KN))
            floor = 0.4 * beat if beat > 0 else 1.0
            _assert(
                reach.min_sog >= floor,
                f"reach min_sog>={floor:.1f} (40% beat; got {reach.min_sog})",
                failures,
            )
    if failures:
        raise GateFailure(f"GATE 5 failed ({len(failures)}): " + "; ".join(failures))
    print("[GATE 5] PASS (acceptance)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", action="store_true", help="Use Solent sample scenarios")
    parser.add_argument(
        "--gate",
        type=int,
        choices=[0, 1, 2, 3, 4, 5],
        default=None,
        help="Run a specific quality gate after scenarios",
    )
    parser.add_argument("--acceptance", action="store_true", help="Full acceptance (gate 5)")
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=list(SCENARIOS.keys()),
        default=None,
    )
    parser.add_argument("--no-baseline", action="store_true", help="Do not write baseline.json")
    parser.add_argument(
        "--buggy-baseline",
        action="store_true",
        help="Write buggy_layline_baseline.json (Phase 1 capture)",
    )
    parser.add_argument(
        "--monotonicity",
        action="store_true",
        help="Run upwind across tack penalties (for gate 3)",
    )
    args = parser.parse_args()
    if not args.sample and args.gate is None and not args.acceptance:
        parser.print_help()
        return 2

    print(f"[validate] sample_bbox={SAMPLE_BBOX}")
    results = run_all(args.scenarios)

    if args.buggy_baseline or args.gate == 1:
        path = save_baseline(results, BUGGY_BASELINE_PATH)
        print(f"[validate] wrote buggy baseline → {path}")

    if not args.no_baseline and (args.gate == 0 or args.gate is None) and not args.buggy_baseline:
        path = save_baseline(results)
        print(f"[validate] wrote baseline → {path}")

    mono: list[tuple[float, ScenarioMetrics]] = []
    if args.monotonicity or args.gate == 3:
        boat = _make_boat()
        union = Course(
            start=(SAMPLE_BBOX["lat_min"] + 0.05, SAMPLE_BBOX["lon_min"] + 0.05),
            finish=(SAMPLE_BBOX["lat_max"] - 0.05, SAMPLE_BBOX["lon_max"] - 0.05),
            depart_utc=DEPART,
            min_depth_m=3.0,
        )
        geo = _build_geo(union)
        for pen in (15.0, 30.0, 60.0, 120.0):
            print(f"[validate] monotonicity tack_penalty_s={pen}")
            m = run_scenario(SCENARIOS["upwind"], boat=boat, geo=geo, tack_penalty_s=pen)
            mono.append((pen, m))
            print(f"  tacks={m.n_tacks} dist={m.distance_nm} elapsed={m.elapsed_s}")

    try:
        if args.gate == 0:
            gate0(results)
        elif args.gate == 1:
            gate1(results)
        elif args.gate == 2:
            gate2(results)
        elif args.gate == 3:
            gate3(results, mono)
        elif args.gate == 4:
            gate4(results)
        elif args.gate == 5 or args.acceptance:
            gate5(results)
    except GateFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
