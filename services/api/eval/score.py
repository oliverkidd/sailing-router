"""Run one eval case: router, reference, feasibility, regret."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from app.boat import Boat
from app.geo import bearing, destination, distance_nm
from app.router import IsochroneRouter, RouterParams
from app.schemas import BoatConfig, Course, Mark, ModelRoute
from app.store import ensure_sample, get_polar
from eval.cases import EvalCase
from eval.geo_build import channel_geo, chart_geo, gap_geo, island_geo, open_geo
from eval.oracle_astar import astar_course
from eval.oracle_exact import Reference, reference_course
from eval.params import DEPART
from eval.wind_synth import SynthWind, build_wind


def load_boat() -> Boat:
    polar_id = ensure_sample()
    return Boat(BoatConfig(polar_id=polar_id), get_polar(polar_id))


@dataclass
class CaseScore:
    id: str
    split: str
    fast: bool
    description: str
    status: str
    feasible: bool
    fails: list[str]
    regret: float | None
    router_elapsed_s: float | None
    ref_s: float | None
    router_runtime_s: float
    oracle_runtime_s: float
    n_tacks: int
    n_gybes: int
    ref_tacks: int | None
    ref_gybes: int | None
    tag: str
    method: str
    error: str | None = None
    extra: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def to_course(
    case: EvalCase,
    *,
    start: tuple[float, float] | None = None,
    marks: tuple[Any, ...] | None = None,
    depart: Any = None,
) -> Course:
    mark_src = case.marks if marks is None else marks
    built = [
        Mark(position=(m.lat, m.lon), radius_nm=m.radius_nm, name=m.name, rounding="either")
        for m in mark_src
    ]
    return Course(
        start=case.start if start is None else start,
        finish=case.finish,
        marks=built,
        exclusions=[],
        depart_utc=DEPART if depart is None else depart,
        min_depth_m=case.min_depth_m,
    )


def _radii(case: EvalCase, params: RouterParams, marks: tuple[Any, ...] | None = None) -> list[float]:
    src = case.marks if marks is None else marks
    return [float(m.radius_nm) for m in src] + [float(params.finish_snap_radius_nm)]


def build_geo(case: EvalCase, params: RouterParams) -> Any:
    if case.geo == "open":
        geo = open_geo(case.points, case.min_depth_m)
    elif case.geo == "island":
        spec = case.island
        if spec is None:
            raise RuntimeError(f"{case.id} is an island case without a size")
        dist = distance_nm(case.start, case.finish)
        center = destination(case.start, bearing(case.start, case.finish), dist / 2.0)
        geo = island_geo(
            case.points,
            center,
            east_nm=spec.east_nm,
            north_nm=spec.north_nm,
            min_depth_m=case.min_depth_m,
        )
    elif case.geo == "gap":
        geo = gap_geo(case.min_depth_m)
    elif case.geo == "channel":
        geo = channel_geo(case.min_depth_m)
    elif case.geo == "chart":
        geo = chart_geo(to_course(case))
    else:
        raise ValueError(case.geo)
    geo.depth_safety_margin_m = params.depth_safety_margin_m
    return geo


def _route(
    boat: Boat,
    wind: SynthWind,
    geo: Any,
    course: Course,
    params: RouterParams,
) -> tuple[ModelRoute, float]:
    t0 = time.perf_counter()
    engine = IsochroneRouter(boat=boat, wind=wind, geo=geo, params=params)
    route = engine.route(course, model_id="eval")
    return route, time.perf_counter() - t0


def _elapsed(route: ModelRoute, depart: Any) -> float:
    if not route.legs:
        return 0.0
    return (route.legs[-1].t_utc - depart).total_seconds()


def _classify_block(geo: Any, a: tuple[float, float], b: tuple[float, float]) -> str:
    land = geo.land
    if land.segment_crosses_land(a, b) or land.on_land(a) or land.on_land(b):
        return "land"
    return "depth"


def feasibility(
    route: ModelRoute,
    course: Course,
    geo: Any,
    params: RouterParams,
) -> list[str]:
    fails: list[str] = []
    if route.forecast_exhausted:
        fails.append("forecast_exhausted")
    if not route.legs:
        fails.append("empty")
        return fails
    if route.unreachable:
        fails.append("unreachable")

    finish_r = float(params.finish_snap_radius_nm)
    last = route.legs[-1].position
    if distance_nm(last, course.finish) > 0.05:
        fails.append("finish_not_reached")
    elif len(route.legs) >= 2:
        approach = route.legs[-2].position
        if distance_nm(approach, course.finish) > finish_r + 0.08:
            fails.append("finish_not_reached")

    for mark in course.marks:
        nearest = min(distance_nm(leg.position, mark.position) for leg in route.legs)
        if nearest > mark.radius_nm + 0.05:
            fails.append(f"mark_not_reached:{mark.name or 'mark'}")

    seen: set[str] = set()
    positions = [leg.position for leg in route.legs]
    for i in range(1, len(positions)):
        a, b = positions[i - 1], positions[i]
        if not geo.blocked_segment(a, b) and not geo.blocked_point(b):
            continue
        kind = _classify_block(geo, a, b)
        if kind not in seen:
            seen.add(kind)
            fails.append(kind)
    return fails


def _blocks(route: ModelRoute, geo: Any, until: Any) -> list[str]:
    """Land and depth hits. ``until`` limits the check to the sailed prefix."""
    fails: list[str] = []
    seen: set[str] = set()
    positions: list[tuple[float, float]] = []
    for leg in route.legs:
        if until is not None and leg.t_utc > until:
            break
        positions.append(leg.position)
    for i in range(1, len(positions)):
        a, b = positions[i - 1], positions[i]
        if not geo.blocked_segment(a, b) and not geo.blocked_point(b):
            continue
        kind = _classify_block(geo, a, b)
        if kind not in seen:
            seen.add(kind)
            fails.append(kind)
    return fails


def _tag(
    *,
    status: str,
    feasible: bool,
    fails: list[str],
    regret: float | None,
    n_tacks: int,
    ref_tacks: int | None,
) -> str:
    if status != "ok":
        return "error"
    if not feasible:
        if "land" in fails:
            return "land"
        if "depth" in fails:
            return "depth"
        return "unreachable"
    if regret is None:
        return "no_reference"
    if regret < -0.01:
        return "beat_oracle"
    if ref_tacks is not None and n_tacks >= ref_tacks + 3 and regret > 0.05:
        return "extra_tacks"
    if regret > 0.08:
        return "slow"
    return "ok"


def _reference(
    case: EvalCase,
    boat: Boat,
    wind: SynthWind,
    geo: Any,
    params: RouterParams,
    points: list[tuple[float, float]] | None = None,
    radii: list[float] | None = None,
) -> tuple[Reference | None, float]:
    if case.ref == "feasibility":
        return None, 0.0
    pts = points if points is not None else case.points
    rad = radii if radii is not None else _radii(case, params)
    t0 = time.perf_counter()
    if case.ref == "exact":
        ref = reference_course(boat, wind, pts, rad)
    elif case.ref == "astar":
        step = 0.2 if case.geo == "island" else 0.25
        ref = astar_course(
            boat,
            wind,
            geo,
            pts,
            rad,
            DEPART,
            step_nm=step,
            cell_nm=0.3,
            heading_step_deg=15.0,
            max_expansions=60_000,
        )
    else:
        raise ValueError(case.ref)
    return ref, time.perf_counter() - t0


def _position_at(
    route: ModelRoute, elapsed_s: float, depart: Any
) -> tuple[tuple[float, float], float]:
    """Boat position after ``elapsed_s`` of the route, and the time actually sailed."""
    legs = route.legs
    if not legs:
        raise RuntimeError("route has no legs")
    target = depart + timedelta(seconds=elapsed_s)
    if legs[-1].t_utc <= target:
        return legs[-1].position, _elapsed(route, depart)
    for i in range(1, len(legs)):
        if legs[i].t_utc < target:
            continue
        a, b = legs[i - 1], legs[i]
        span = (b.t_utc - a.t_utc).total_seconds()
        if span <= 1e-6:
            return b.position, (b.t_utc - depart).total_seconds()
        frac = ((target - a.t_utc).total_seconds()) / span
        frac = min(1.0, max(0.0, frac))
        lat = a.position[0] + frac * (b.position[0] - a.position[0])
        lon = a.position[1] + frac * (b.position[1] - a.position[1])
        return (lat, lon), elapsed_s
    return legs[-1].position, _elapsed(route, depart)


def _marks_remaining(case: EvalCase, route: ModelRoute, until: Any) -> list[Any]:
    left = []
    for mark in case.marks:
        rounded = False
        for leg in route.legs:
            if leg.t_utc > until:
                break
            if distance_nm(leg.position, (mark.lat, mark.lon)) <= mark.radius_nm + 0.05:
                rounded = True
                break
        if not rounded:
            left.append(mark)
    return left


def _score_from_route(
    case: EvalCase,
    route: ModelRoute,
    depart: Any,
    router_s: float,
    ref: Reference | None,
    oracle_s: float,
    fails: list[str],
    extra: dict[str, float],
) -> CaseScore:
    elapsed = _elapsed(route, depart)
    feasible = not fails
    regret = None
    if ref is not None and ref.elapsed_s > 1.0 and feasible:
        regret = (elapsed - ref.elapsed_s) / ref.elapsed_s
    ref_tacks = None if ref is None else ref.n_tacks
    status = "ok"
    return CaseScore(
        id=case.id,
        split=case.split,
        fast=case.fast,
        description=case.description,
        status=status,
        feasible=feasible,
        fails=fails,
        regret=regret,
        router_elapsed_s=elapsed,
        ref_s=None if ref is None else ref.elapsed_s,
        router_runtime_s=router_s,
        oracle_runtime_s=oracle_s,
        n_tacks=route.n_tacks,
        n_gybes=route.n_gybes,
        ref_tacks=ref_tacks,
        ref_gybes=None if ref is None else ref.n_gybes,
        tag=_tag(
            status=status,
            feasible=feasible,
            fails=fails,
            regret=regret,
            n_tacks=route.n_tacks,
            ref_tacks=ref_tacks,
        ),
        method="none" if ref is None else ref.method,
        extra=extra,
    )


def _error_score(case: EvalCase, message: str, router_s: float = 0.0) -> CaseScore:
    return CaseScore(
        id=case.id,
        split=case.split,
        fast=case.fast,
        description=case.description,
        status="error",
        feasible=False,
        fails=[message],
        regret=None,
        router_elapsed_s=None,
        ref_s=None,
        router_runtime_s=router_s,
        oracle_runtime_s=0.0,
        n_tacks=0,
        n_gybes=0,
        ref_tacks=None,
        ref_gybes=None,
        tag="error",
        method="",
        error=message,
    )


def run_case(case: EvalCase, boat: Boat, params: RouterParams) -> CaseScore:
    try:
        geo = build_geo(case, params)
    except Exception as exc:  # noqa: BLE001 — one bad chart must not kill the suite
        return _error_score(case, f"geo_unavailable: {exc}")

    if case.protocol == "forecast_update":
        return _run_forecast(case, boat, params, geo)

    wind = build_wind(case.wind, DEPART)
    try:
        ref, oracle_s = _reference(case, boat, wind, geo, params)
    except Exception as exc:  # noqa: BLE001
        return _error_score(case, f"oracle_failed: {exc}")
    try:
        route, router_s = _route(boat, wind, geo, to_course(case), params)
    except Exception as exc:  # noqa: BLE001
        return _error_score(case, f"router_failed: {exc}")
    fails = feasibility(route, to_course(case), geo, params)
    return _score_from_route(case, route, DEPART, router_s, ref, oracle_s, fails, {})


def _run_forecast(case: EvalCase, boat: Boat, params: RouterParams, geo: Any) -> CaseScore:
    if case.forecast_wind is None or case.update_after_s is None:
        return _error_score(case, "forecast case is missing a forecast wind")
    truth = build_wind(case.wind, DEPART)
    forecast = build_wind(case.forecast_wind, DEPART)
    try:
        ref, oracle_s = _reference(case, boat, truth, geo, params)
    except Exception as exc:  # noqa: BLE001
        return _error_score(case, f"oracle_failed: {exc}")

    course = to_course(case)
    try:
        first, t_first = _route(boat, forecast, geo, course, params)
    except Exception as exc:  # noqa: BLE001
        return _error_score(case, f"router_failed: {exc}")

    extra: dict[str, float] = {}
    try:
        stubborn, t_stub = _route(boat, forecast, geo, course, params)
        extra["stubborn_runtime_s"] = t_stub
        if not feasibility(stubborn, course, geo, params):
            extra["stubborn_infeasible"] = 1.0
        else:
            extra["stubborn_s"] = _elapsed(stubborn, DEPART)
    except Exception:  # noqa: BLE001
        stubborn = None

    update = case.update_after_s
    if not first.legs:
        return _error_score(case, "router_failed: empty forecast route", t_first)

    pos, sailed = _position_at(first, update, DEPART)
    if sailed + 1.0 < update or distance_nm(pos, case.finish) <= params.finish_snap_radius_nm:
        # Finished (or stalled) before the forecast was replaced.
        fails = feasibility(first, course, geo, params)
        if stubborn is not None and "stubborn_s" in extra and not fails:
            extra["update_gain_s"] = extra["stubborn_s"] - _elapsed(first, DEPART)
        return _score_from_route(case, first, DEPART, t_first, ref, oracle_s, fails, extra)

    cut = DEPART + timedelta(seconds=sailed)
    remaining = _marks_remaining(case, first, cut)
    course2 = to_course(case, start=pos, marks=tuple(remaining), depart=cut)
    try:
        second, t_second = _route(boat, truth, geo, course2, params)
    except Exception as exc:  # noqa: BLE001
        return _error_score(case, f"router_failed: {exc}", t_first)

    # Time on the forecast, then time on the corrected wind. Land and depth are
    # checked only on the track the boat actually sailed.
    total_s = sailed + _elapsed(second, cut)
    fails = _blocks(first, geo, cut)
    fails.extend(_blocks(second, geo, None))
    fails.extend(_marks_of_stitched(case, first, second, cut, params))

    feasible = not fails
    regret = None
    if ref is not None and ref.elapsed_s > 1.0 and feasible:
        regret = (total_s - ref.elapsed_s) / ref.elapsed_s
    if "stubborn_s" in extra and feasible:
        extra["update_gain_s"] = extra["stubborn_s"] - total_s

    # Present the stitched time through a thin stand-in: reuse `second` counts
    # plus the first route's maneuvers, and override elapsed via the score fields.
    score = _score_from_route(
        case,
        second,
        cut,
        t_first + t_second,
        ref,
        oracle_s,
        fails,
        extra,
    )
    score.router_elapsed_s = total_s
    score.n_tacks = first.n_tacks + second.n_tacks
    score.n_gybes = first.n_gybes + second.n_gybes
    score.feasible = feasible
    score.regret = regret
    score.tag = _tag(
        status="ok",
        feasible=feasible,
        fails=fails,
        regret=regret,
        n_tacks=score.n_tacks,
        ref_tacks=score.ref_tacks,
    )
    return score


def _marks_of_stitched(
    case: EvalCase,
    first: ModelRoute,
    second: ModelRoute,
    cut: Any,
    params: RouterParams,
) -> list[str]:
    fails: list[str] = []
    positions: list[tuple[float, float]] = []
    for leg in first.legs:
        if leg.t_utc > cut:
            break
        positions.append(leg.position)
    positions.extend(leg.position for leg in second.legs)
    for mark in case.marks:
        if not positions:
            fails.append(f"mark_not_reached:{mark.name or 'mark'}")
            continue
        nearest = min(distance_nm(p, (mark.lat, mark.lon)) for p in positions)
        if nearest > mark.radius_nm + 0.05:
            fails.append(f"mark_not_reached:{mark.name or 'mark'}")
    if second.legs and distance_nm(second.legs[-1].position, case.finish) > 0.05:
        fails.append("finish_not_reached")
    elif len(second.legs) >= 2:
        approach = second.legs[-2].position
        if distance_nm(approach, case.finish) > params.finish_snap_radius_nm + 0.08:
            fails.append("finish_not_reached")
    return fails
