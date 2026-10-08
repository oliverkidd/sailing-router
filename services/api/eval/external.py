"""Second clock: libweatherrouting's isochrone, on our wind, polar, and chart.

The package is GPL-3 and is imported only from this eval module. It is not
part of the sailing app. Install it in the API venv when you want the
comparison:

    .venv/bin/python -m pip install weatherrouting

Bitsailor is a Common Lisp program and the OpenCPN weather-routing plugin is
a desktop chart application. Neither can be called on these synthetic cases
without that whole stack, so they are not wired in.

On a proved case the closed-form time stays the answer. This router is a
second feasible time: if it is legal and faster than ours, that gap is a real
improvement target. If it is slower than the proved time, we do not chase it.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.boat import Boat
from app.geo import distance_nm
from eval.cases import EvalCase
from eval.params import DEPART
from eval.score import build_geo, to_course
from eval.wind_synth import SynthWind, build_wind

# 1 knot in metres per second. libweatherrouting converts m/s back to knots.
_KN_TO_MS = 0.514444
_STEP_H = 0.1  # 6 minutes
_MAX_STEPS = 80
_FINISH_NM = 0.4


def available() -> bool:
    try:
        import weatherrouting  # noqa: F401
    except ImportError:
        return False
    return True


def write_polar(boat: Boat, path: Path) -> Path:
    """Their polar file: TWS across the header, one TWA per row, speeds in knots."""
    table = boat.polar
    tws_vals = [float(t) for t in table.tws]
    twa_vals = [float(t) for t in table.twa]
    speeds = [[float(table.speed[j, i]) for j in range(len(tws_vals))] for i in range(len(twa_vals))]
    # Their interpolator extrapolates below the first angle. A polar that
    # starts at 40° with zero speed otherwise produces a large negative speed
    # in the no-go zone, and the boat sails backwards.
    if not twa_vals or twa_vals[0] > 0.5:
        twa_vals.insert(0, 0.0)
        speeds.insert(0, [0.0] * len(tws_vals))
    header = "TWA\\TWS " + " ".join(f"{t:.4g}" for t in tws_vals)
    lines = [header]
    for twa, row in zip(twa_vals, speeds, strict=True):
        lines.append(f"{twa:.4g} " + " ".join(f"{s:.4g}" for s in row))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class _Grib:
    def __init__(self, wind: SynthWind) -> None:
        self.wind = wind

    def get_wind_at(self, t, lat: float, lon: float):  # noqa: ANN001 — their call shape
        sampled = self.wind.wind_at(lat, lon, t)
        if sampled is None:
            raise RuntimeError("wind exhausted")
        tws_kn, twd = sampled
        return (float(twd), float(tws_kn) * _KN_TO_MS)


def route_case(
    case: EvalCase,
    boat: Boat,
    *,
    polar_path: Path,
    step_h: float = _STEP_H,
) -> dict[str, Any]:
    """Run LinearBestIsoRouter. Returns elapsed time, legality, and wall clock."""
    from weatherrouting import Polar, Routing
    from weatherrouting.routers.linearbestisorouter import LinearBestIsoRouter

    geo = build_geo(case, _params())
    truth = build_wind(case.wind, DEPART)
    # The isochrone sails the wind it is given. For a forecast update that is
    # the truth field, same as perfect foresight, so the comparison is the
    # single-shot route on that field.
    course = to_course(case)
    track = [tuple(m.position) for m in course.marks] + [tuple(course.finish)]

    def point_ok(lat: float, lon: float) -> bool:
        return not geo.blocked_point((lat, lon))

    def line_ok(lat1: float, lon1: float, lat2: float, lon2: float) -> bool:
        return not geo.blocked_segment((lat1, lon1), (lat2, lon2))

    polar = Polar(str(polar_path))
    routing = Routing(
        LinearBestIsoRouter,
        polar,
        track,
        _Grib(truth),
        DEPART,
        start_position=tuple(course.start),
        point_validity=point_ok,
        line_validity=line_ok,
    )
    routing.algorithm.set_param_value("min_increase", _FINISH_NM)
    routing.algorithm.set_param_value("subdiv", 5)

    t0 = time.perf_counter()
    error = None
    try:
        steps = 0
        while not routing.end and steps < _MAX_STEPS:
            routing.step(timedelta=step_h)
            steps += 1
    except Exception as exc:  # noqa: BLE001 — their router raises RoutingNoWindError and friends
        error = f"{type(exc).__name__}: {exc}"
        steps = 0
    runtime = time.perf_counter() - t0

    path = list(routing.path)
    elapsed = None
    if path and getattr(path[-1], "time", None) is not None:
        elapsed = (path[-1].time - DEPART).total_seconds()
    positions = [(p.pos[0], p.pos[1]) for p in path if getattr(p, "pos", None)]
    reached = False
    if positions:
        reached = distance_nm(positions[-1], course.finish) <= _FINISH_NM + 0.15
    fails: list[str] = []
    if error:
        fails.append(error)
    if not reached:
        fails.append("finish_not_reached")
    for i in range(1, len(positions)):
        if geo.blocked_segment(positions[i - 1], positions[i]) or geo.blocked_point(positions[i]):
            if "land_or_depth" not in fails:
                fails.append("land_or_depth")
            break
    feasible = reached and not fails
    return {
        "router": "libweatherrouting",
        "algorithm": "LinearBestIsoRouter",
        "elapsed_s": None if elapsed is None else round(elapsed, 1),
        "feasible": feasible,
        "fails": fails,
        "n_points": len(positions),
        "steps": steps if not error else 0,
        "runtime_s": round(runtime, 3),
        "step_h": step_h,
    }


def _params():
    from eval.params import product_params

    return product_params()


def compare(our_elapsed: float | None, their: dict[str, Any], proved_s: float | None) -> dict[str, Any]:
    """Gaps versus our router and versus the proved time, when both exist."""
    out: dict[str, Any] = {}
    theirs = their.get("elapsed_s")
    if (
        our_elapsed is not None
        and theirs
        and their.get("feasible")
        and our_elapsed > 0
    ):
        out["our_minus_theirs_rel"] = (our_elapsed - float(theirs)) / float(theirs)
    if proved_s and theirs and their.get("feasible") and proved_s > 0:
        out["theirs_minus_proved_rel"] = (float(theirs) - proved_s) / proved_s
    return out
