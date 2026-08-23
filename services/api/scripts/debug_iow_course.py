#!/usr/bin/env python3
"""Reproduce the user's IoW course (icon_eu) and diagnose M1→M2 frontier_empty.

Usage (from services/api):
  .venv/bin/python scripts/debug_iow_course.py
  .venv/bin/python scripts/debug_iow_course.py --synthetic-wind   # no GRIB fetch
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

from app import store  # noqa: E402
from app.boat import Boat  # noqa: E402
from app.field_service import ensure_wind_window  # noqa: E402
from app.geo import (  # noqa: E402
    COURSE_WIND_MARGIN_DEG,
    GeoContext,
    bearing,
    course_bbox,  # noqa: E402
    destination,
    distance_nm,
)
from app.geo.fetch import fetch_depth_for_bbox, fetch_land_for_bbox  # noqa: E402
from app.navmath import signed_diff, twa_from  # noqa: E402
from app.router import IsochroneRouter, Node, RouterParams  # noqa: E402
from app.schemas import BoatConfig, Course, Mark, ModelRoute, RouteLeg  # noqa: E402
from app.util.horizon import initial_route_horizon_h  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("debug_iow")


def _dm(deg: float, minutes: float, hemi: str) -> float:
    """Degrees + decimal minutes → signed decimal degrees."""
    sign = -1.0 if hemi.upper() in ("S", "W") else 1.0
    return sign * (deg + minutes / 60.0)


# Course from UI screenshot (20/07/2026 21:00 UTC).
START = (_dm(50, 46.784, "N"), _dm(1, 18.767, "W"))
M1 = (_dm(50, 40.148, "N"), _dm(1, 38.437, "W"))
M2 = (_dm(50, 32.997, "N"), _dm(1, 21.013, "W"))
M3 = (_dm(50, 30.019, "N"), _dm(1, 42.456, "W"))
M4 = (_dm(50, 28.623, "N"), _dm(1, 10.090, "W"))
M5 = (_dm(50, 31.215, "N"), _dm(1, 14.322, "W"))
FINISH = (_dm(50, 39.765, "N"), _dm(0, 58.040, "W"))
DEPART = datetime(2026, 7, 20, 21, 0, 0, tzinfo=UTC)


def _probe_rhumb(geo: GeoContext, a: tuple[float, float], b: tuple[float, float], n: int = 40) -> None:
    brg = bearing(a, b)
    dist = distance_nm(a, b)
    print("\n=== Rhumb M1→M2 ===")
    print(f"  from {a} → {b}")
    print(f"  dist={dist:.2f}nm bearing={brg:.1f}°")
    hits = 0
    for i in range(n + 1):
        f = i / n
        p = destination(a, brg, dist * f)
        on_land = geo.land.on_land(p) if geo.land is not None else False
        depth = geo.depth.depth(p) if geo.depth is not None else None
        blocked_pt = geo.blocked_point(p)
        if blocked_pt or on_land:
            hits += 1
            if hits <= 8:
                d_s = f"{depth:.1f}m" if depth is not None else "?"
                print(
                    f"  BLOCK @{f:.0%} lat={p[0]:.4f} lon={p[1]:.4f} "
                    f"land={on_land} depth={d_s} blocked_pt={blocked_pt}"
                )
    if hits == 0:
        print("  OK: no land/depth blocks along rhumb (sample points)")
    else:
        print(f"  FAIL: {hits}/{n + 1} sample points blocked along rhumb")
    # Segment check (same as router probe).
    mid = destination(a, brg, min(1.0, dist * 0.5))
    seg = geo.blocked_segment(a, mid)
    print(f"  blocked_segment(start→1nm or mid)={seg}")


def _max_xte_nm(
    positions: list[tuple[float, float]],
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    """Max cross-track error (nm) of positions vs rhumb a→b."""
    brg = bearing(a, b)
    max_xte = 0.0
    for p in positions:
        d_a = distance_nm(a, p)
        if d_a < 1e-6:
            continue
        brg_p = bearing(a, p)
        xte = abs(d_a * math.sin(math.radians(signed_diff(brg_p, brg))))
        if xte > max_xte:
            max_xte = xte
    return max_xte


def _split_legs_at_marks(legs: list[RouteLeg]) -> list[list[RouteLeg]]:
    """Split route polyline into per-waypoint segments ending at each mark snap."""
    segs: list[list[RouteLeg]] = []
    cur: list[RouteLeg] = []
    for leg in legs:
        cur.append(leg)
        if leg.maneuver == "mark":
            segs.append(cur)
            cur = [leg]
    if len(cur) > 1:
        segs.append(cur)
    return segs


def _print_leg_summaries(
    engine: IsochroneRouter,
    route: ModelRoute,
    waypoints: list[tuple[str, tuple[float, float]]],
    *,
    focus: set[str] | None = None,
) -> None:
    """Per-leg seed mode / candidates / rhumb XTE for focus marks (default M1–M3)."""
    focus = focus or {"M1", "M2", "M3"}
    segs = _split_legs_at_marks(route.legs)
    print("\n=== Per-leg seed / XTE (M1–M3) ===")
    for i, seg in enumerate(segs):
        if i + 1 >= len(waypoints):
            break
        name_from, pos_from = waypoints[i]
        name_to, pos_to = waypoints[i + 1]
        if name_to not in focus:
            continue
        positions = [tuple(l.position) for l in seg]
        first = seg[0]
        node = Node(
            lat=first.position[0],
            lon=first.position[1],
            t=first.t_utc,
            heading=None,
            rethink=True,
            maneuver="mark" if i > 0 else "none",
        )
        w = engine.wind.wind_at(node.lat, node.lon, node.t)
        if w is None:
            print(f"  {name_from}→{name_to}: wind=NONE at start")
            continue
        tws, twd = w
        seed, mode, twa_brg = engine._seed_heading(node, tws, twd, pos_to)
        cands = engine._candidate_headings(node, tws, twd, pos_to)
        xte = _max_xte_nm(positions, pos_from, pos_to)
        rhumb_nm = distance_nm(pos_from, pos_to)
        track_nm = 0.0
        for a, b in zip(positions, positions[1:]):
            track_nm += distance_nm(a, b)
        n_tack = sum(1 for l in seg if l.maneuver == "tack")
        n_gybe = sum(1 for l in seg if l.maneuver == "gybe")
        print(
            f"  {name_from}→{name_to}: seed={mode} seed_hdg={seed:.0f} "
            f"twa_brg={twa_brg:.0f} cands={len(cands)} "
            f"rhumb={rhumb_nm:.2f}nm track={track_nm:.2f}nm max_xte={xte:.2f}nm "
            f"tacks={n_tack} gybes={n_gybe} pts={len(seg)}"
        )


def _diag_node(
    engine: IsochroneRouter,
    node: Node,
    tgt: tuple[float, float],
    *,
    label: str,
) -> None:
    w = engine.wind.wind_at(node.lat, node.lon, node.t)
    if w is None:
        print(f"  [{label}] wind=NONE at {node.pos} t={node.t.isoformat()}")
        return
    tws, twd = w
    brg = bearing(node.pos, tgt)
    rem = distance_nm(node.pos, tgt)
    up = engine.boat.target_upwind_twa(tws)
    dn = engine.boat.target_downwind_twa(tws)
    mark_clear = engine._mark_bearing_clear(node.pos, tgt, tws=tws, twd=twd)
    seed, mode, twa_brg = engine._seed_heading(node, tws, twd, tgt)
    cands = engine._candidate_headings(node, tws, twd, tgt)
    print(
        f"  [{label}] pos=({node.lat:.4f},{node.lon:.4f}) rem={rem:.2f} "
        f"hdg={node.heading} rethink={node.rethink} man={node.maneuver}"
    )
    print(
        f"         tws={tws:.1f} twd={twd:.0f} brg={brg:.0f} "
        f"twa_brg={twa_brg:.0f} seed={mode}@{seed:.0f} up={up:.0f} dn={dn:.0f} "
        f"mark_clear={mark_clear} blocked_pt={engine.geo.blocked_point(node.pos)} "
        f"cands={len(cands)}"
    )
    beat = engine.boat.bsp(tws, up)
    floor = engine.p.min_bsp_fraction_of_beat * beat if beat > 0 else 0.0
    ok = geo_none = bsp0 = floor_n = other = 0
    samples: list[str] = []
    for h in cands[:24]:
        twa = twa_from(h, twd)
        bsp = engine.boat.bsp(tws, twa)
        if bsp <= 0:
            bsp0 += 1
            samples.append(f"h={h:.0f}:bsp0")
            continue
        if floor > 0 and bsp < floor:
            floor_n += 1
            samples.append(f"h={h:.0f}:floor({bsp:.1f}<{floor:.1f})")
            continue
        # Probe commit budgets / early exits.
        man = "n/a"
        if node.heading is not None:
            from app.router import _course_change_penalty_s, _detect_maneuver

            man = _detect_maneuver(node, h, twd, twa)
            pen = 0.0
            if man == "tack":
                pen = engine.boat.config.tack_penalty_s
            elif man == "gybe":
                pen = engine.boat.config.gybe_penalty_s
            elif node.maneuver != "mark" and not node.rethink:
                pen = _course_change_penalty_s(
                    signed_diff(h, node.heading), tws, engine.boat.config.tack_penalty_s
                )
        else:
            pen = 0.0
        child, geo_hit = engine._commit_heading(
            node, h, tgt, max_commit_s=60.0, tws0=tws, twd0=twd
        )
        child2, geo2 = engine._commit_heading(
            node, h, tgt, max_commit_s=3600.0, tws0=tws, twd0=twd
        )
        if child is None and child2 is None:
            if geo_hit or geo2:
                geo_none += 1
                samples.append(f"h={h:.0f}:geo man={man} pen={pen:.0f} bsp={bsp:.2f}")
            else:
                other += 1
                samples.append(
                    f"h={h:.0f}:other man={man} pen={pen:.0f} bsp={bsp:.2f} "
                    f"twa={twa:.0f}"
                )
        else:
            ok += 1
            c = child or child2
            assert c is not None
            dmg = rem - distance_nm(c.pos, tgt)
            samples.append(
                f"h={h:.0f}:okΔ={dmg:+.2f} man={man} pen={pen:.0f} bsp={bsp:.2f}"
            )
    print(
        f"         try≤24 → ok={ok} geo_none={geo_none} bsp0={bsp0} "
        f"floor={floor_n} other={other}"
    )
    print(f"         samples={samples[:12]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--synthetic-wind",
        action="store_true",
        help="Use constant 12kn from SW instead of icon_eu",
    )
    args = ap.parse_args()

    print("Course (decimal deg):")
    for name, p in [
        ("S", START),
        ("M1", M1),
        ("M2", M2),
        ("M3", M3),
        ("M4", M4),
        ("M5", M5),
        ("F", FINISH),
    ]:
        print(f"  {name}: ({p[0]:.4f}, {p[1]:.4f})")

    course = Course(
        start=START,
        finish=FINISH,
        marks=[
            Mark(position=M1, radius_nm=0.1, name="M1"),
            Mark(position=M2, radius_nm=0.1, name="M2"),
            Mark(position=M3, radius_nm=0.1, name="M3"),
            Mark(position=M4, radius_nm=0.1, name="M4"),
            Mark(position=M5, radius_nm=0.1, name="M5"),
        ],
        exclusions=[],
        depart_utc=DEPART,
        min_depth_m=3.0,
    )

    bbox = course_bbox(course, margin_deg=COURSE_WIND_MARGIN_DEG)
    print(f"\nFetching land/depth for {bbox} …")
    land = fetch_land_for_bbox(bbox)
    depth = fetch_depth_for_bbox(bbox, land)
    geo = GeoContext.build(
        land=land,
        depth=depth,
        exclusion_rings=[],
        min_depth_m=course.min_depth_m,
    )
    _probe_rhumb(geo, M1, M2)

    polar_id = store.ensure_sample()
    polar = store.get_polar(polar_id)
    boat = Boat(BoatConfig(polar_id=polar_id), polar)

    if args.synthetic_wind:
        from scripts.validate_routing import SyntheticWindField

        # SW wind ≈ from 225 (matches map arrows toward NE).
        wind = SyntheticWindField(tws=12.0, twd=225.0)
        print("\nWind: synthetic 12kn from 225°")
    else:
        horizon_h = initial_route_horizon_h()
        end = DEPART + timedelta(hours=horizon_h)
        print(f"\nFetching icon_eu wind {DEPART.isoformat()} → +{horizon_h}h …")
        wind = ensure_wind_window("icon_eu", bbox, start=DEPART, end=end, pad_region=False)
        if wind is None:
            print("ERROR: icon_eu wind unavailable")
            return 2
        print(f"Wind: icon_eu n_times={wind.n_times}")

    events: list[dict] = []

    def on_progress(ev: dict) -> None:
        events.append(ev)
        if ev.get("type") == "leg":
            print(
                f"  event leg {ev.get('index')}/{ev.get('total')} "
                f"{ev.get('name')} {ev.get('status')} "
                f"term={ev.get('termination')} steps={ev.get('steps')} "
                f"blocked={ev.get('blocked_calls')} exp={ev.get('expansions')}"
            )

    print("\n=== Routing (with forecast extend like /route) ===")
    engine = IsochroneRouter(
        boat=boat,
        wind=wind,
        geo=geo,
        params=RouterParams(),
        on_progress=on_progress,
        model_id="icon_eu",
    )

    from app.routes.route import _EXTEND_H, _MAX_EXTEND_ROUNDS

    route = None
    for extend_round in range(_MAX_EXTEND_ROUNDS + 1):
        resume = extend_round > 0 and engine.can_resume
        route = engine.route(course, "icon_eu", resume=resume)
        print(
            f"  round={extend_round} resume={resume} "
            f"exhausted={route.forecast_exhausted} unreachable={route.unreachable} "
            f"legs={len(route.legs)} warnings={route.warnings}"
        )
        if not route.forecast_exhausted:
            break
        if extend_round >= _MAX_EXTEND_ROUNDS or not engine.can_resume:
            print("  give up: max extend or cannot resume")
            break
        if args.synthetic_wind:
            print("  synthetic wind — cannot extend; stop")
            break
        t_last = datetime.fromtimestamp(float(wind.times[-1]), UTC)
        t_ext_end = t_last + timedelta(hours=_EXTEND_H)
        print(f"  extending icon_eu +{_EXTEND_H}h from {t_last.isoformat()} …")
        ensure_wind_window("icon_eu", bbox, start=t_last, end=t_ext_end, pad_region=False)
        wind = ensure_wind_window(
            "icon_eu", bbox, start=DEPART, end=t_ext_end, pad_region=False
        )
        if wind is None:
            print("  extend failed: wind None")
            break
        engine.wind = wind
        print(f"  wind now n_times={wind.n_times}")

    assert route is not None
    print(
        f"\nResult: unreachable={route.unreachable} exhausted={route.forecast_exhausted} "
        f"legs={len(route.legs)} dist={route.distance_nm}nm elapsed={route.elapsed_s}s "
        f"tacks={route.n_tacks} gybes={route.n_gybes}"
    )
    for w in route.warnings:
        print(f"  warning: {w}")
    if route.legs:
        last = route.legs[-1].position
        print(f"  last pos=({last[0]:.4f},{last[1]:.4f})")

    waypoints = [
        ("S", START),
        ("M1", M1),
        ("M2", M2),
        ("M3", M3),
        ("M4", M4),
        ("M5", M5),
        ("F", FINISH),
    ]
    if route.legs:
        _print_leg_summaries(engine, route, waypoints, focus={"M1", "M2", "M3"})
        # Seed diagnostics at start of M1→M2 and M2→M3 (post-mark rethink).
        segs = _split_legs_at_marks(route.legs)
        for idx, tgt, label in ((1, M2, "M1→M2 start"), (2, M3, "M2→M3 start")):
            if idx >= len(segs):
                break
            first = segs[idx][0]
            node = Node(
                lat=first.position[0],
                lon=first.position[1],
                t=first.t_utc,
                heading=None,
                rethink=True,
                maneuver="mark",
            )
            _diag_node(engine, node, tgt, label=label)

    return 1 if route.unreachable else 0


if __name__ == "__main__":
    raise SystemExit(main())
