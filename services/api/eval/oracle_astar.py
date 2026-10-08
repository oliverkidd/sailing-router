"""Time-dependent A* on the same boat, wind, and geo masks as the router.

State is a grid cell, tack side, and heading bin, so a fast but pointless
heading cannot erase a slower VMG heading in the same cell. On uniform wind the
heuristic is the analytic VMG time, which keeps the search on that family of
paths. Elsewhere it is remaining distance over the polar's top speed.
"""

from __future__ import annotations

import heapq
import math
from datetime import datetime, timedelta

from app.boat import Boat
from app.geo import GeoContext, bearing, destination, distance_nm
from app.navmath import norm360, signed_diff, twa_from
from app.router import _wind_side
from eval.oracle_exact import Reference, formula_time, maneuver_penalty, time_on_heading, vmg_angles
from eval.wind_synth import SynthWind


def _max_bsp(boat: Boat, tws: float) -> float:
    best = 0.0
    angle = 0
    while angle <= 180:
        best = max(best, boat.bsp(tws, float(angle)))
        angle += 3
    return max(best, 0.5)


def _uniq(headings: list[float]) -> list[float]:
    out: list[float] = []
    for h in headings:
        h = norm360(h)
        if all(abs(signed_diff(h, o)) > 0.4 for o in out):
            out.append(h)
    return out


def _headings(
    boat: Boat,
    tws: float,
    twd: float,
    pos: tuple[float, float],
    goal: tuple[float, float],
    step_deg: float,
) -> list[float]:
    raw: list[float] = []
    h = 0.0
    while h < 360.0 - 1e-6:
        raw.append(h)
        h += step_deg
    up, dn = vmg_angles(boat, tws)
    raw.extend(
        [
            norm360(twd + up),
            norm360(twd - up),
            norm360(twd + dn),
            norm360(twd - dn),
            bearing(pos, goal),
        ]
    )
    return _uniq(raw)


def astar_leg(
    boat: Boat,
    wind: SynthWind,
    geo: GeoContext,
    start: tuple[float, float],
    finish: tuple[float, float],
    radius_nm: float,
    depart: datetime,
    t_start: float,
    *,
    step_nm: float = 0.12,
    cell_nm: float = 0.3,
    heading_step_deg: float = 10.0,
    max_expansions: int = 80_000,
    window_nm: float | None = None,
) -> tuple[Reference, int]:
    """Return the best time to enter the finish circle, and how many nodes expanded."""
    # A step must leave the cell it starts in. If the cell is larger than the
    # step, the first arrival dominates and the search never crosses the cell.
    cell_nm = min(cell_nm, step_nm * 0.45)
    h_bsp = _max_bsp(boat, wind.tws_ceiling)
    dist0 = distance_nm(start, finish)
    window = window_nm if window_nm is not None else dist0 * 1.4 + 5.0
    lat0, lon0 = start
    cos_lat = max(0.2, math.cos(math.radians(lat0)))
    use_formula = not wind.time_varying and not wind.space_varying

    def cell_of(pos: tuple[float, float]) -> tuple[int, int]:
        north = (pos[0] - lat0) * 60.0
        east = (pos[1] - lon0) * 60.0 * cos_lat
        return int(math.floor(east / cell_nm)), int(math.floor(north / cell_nm))

    def heuristic(pos: tuple[float, float], when: datetime) -> float:
        dist = distance_nm(pos, finish)
        if dist <= radius_nm:
            return 0.0
        if use_formula:
            sampled_h = wind.wind_at(pos[0], pos[1], when)
            if sampled_h is not None:
                tws_h, twd_h = sampled_h
                # Analytic remaining time. Close enough to consistent that A* stays
                # on the optimal headings instead of exploring the whole cone.
                return formula_time(boat, tws_h, twd_h, pos, finish, radius_nm).elapsed_s
        return dist / h_bsp * 3600.0

    def outside(pos: tuple[float, float]) -> bool:
        north = (pos[0] - lat0) * 60.0
        east = (pos[1] - lon0) * 60.0 * cos_lat
        return math.hypot(east, north) > window

    bucket_s = 300.0 if wind.time_varying else 0.0

    def heading_bin(heading: float | None) -> int:
        if heading is None or (isinstance(heading, float) and math.isnan(heading)):
            return -1
        return int(round(norm360(heading) / 15.0)) % 24

    def key_of(pos: tuple[float, float], side: int, g: float, heading: float | None) -> tuple[int, ...]:
        ci, cj = cell_of(pos)
        bucket = int(g / bucket_s) if bucket_s else 0
        return ci, cj, side, heading_bin(heading), bucket

    # (f, seq, g, lat, lon, heading or nan, twd)
    heap: list[tuple[float, int, float, float, float, float, float]] = []
    seq = 0
    best: dict[tuple[int, ...], float] = {}
    best_finish = math.inf
    t0 = depart + timedelta(seconds=t_start)
    w0 = wind.wind_at(start[0], start[1], t0)
    if w0 is None:
        raise RuntimeError("A* wind is empty at the start")
    h0 = heuristic(start, t0)
    heapq.heappush(heap, (h0, seq, 0.0, start[0], start[1], math.nan, w0[1]))
    best[key_of(start, 0, 0.0, None)] = 0.0
    expansions = 0

    while heap:
        f, _, g, lat, lon, heading_f, prev_twd = heapq.heappop(heap)
        if best_finish < math.inf and f > best_finish + 20.0:
            # Keep searching paths that are tied. On a beat every tack point
            # has the same VMG time, so stopping at the first finish keeps a
            # slightly worse grid path.
            break
        pos = (lat, lon)
        prev_h = None if math.isnan(heading_f) else heading_f
        side = 0 if prev_h is None else int(_wind_side(prev_h, prev_twd))
        k = key_of(pos, side, g, prev_h)
        if g > best.get(k, math.inf) + 0.5:
            continue
        if distance_nm(pos, finish) <= radius_nm:
            if g < best_finish:
                best_finish = g
            continue
        expansions += 1
        if expansions > max_expansions:
            break

        now = depart + timedelta(seconds=t_start + g)
        sampled = wind.wind_at(lat, lon, now)
        if sampled is None:
            continue
        tws, twd = sampled
        for heading in _headings(boat, tws, twd, pos, finish, heading_step_deg):
            twa = twa_from(heading, twd)
            bsp = boat.bsp(tws, twa)
            if bsp <= 0.05:
                continue
            _kind, pen = maneuver_penalty(boat, prev_h, prev_twd, heading, twd, now)
            hit = time_on_heading(boat, tws, twd, pos, heading, finish, radius_nm)
            sail_budget = step_nm / bsp * 3600.0
            if hit is not None and hit <= sail_budget + 1e-6:
                hit_nm = bsp * hit / 3600.0
                hit_pos = pos if hit_nm < 1e-4 else destination(pos, heading, hit_nm)
                if not geo.blocked_segment(pos, hit_pos) and not geo.blocked_point(hit_pos):
                    g_hit = g + pen + hit
                    if g_hit < best_finish:
                        best_finish = g_hit
                continue
            nxt = destination(pos, heading, step_nm)
            if outside(nxt) or geo.blocked_segment(pos, nxt) or geo.blocked_point(nxt):
                continue
            g2 = g + pen + sail_budget
            new_side = int(_wind_side(heading, twd))
            k2 = key_of(nxt, new_side, g2, heading)
            # Same cell on the same heading is not progress.
            if k2[0] == k[0] and k2[1] == k[1] and k2[3] == k[3]:
                continue
            if g2 >= best.get(k2, math.inf) - 0.5:
                continue
            best[k2] = g2
            seq += 1
            heapq.heappush(
                heap,
                (g2 + heuristic(nxt, now), seq, g2, nxt[0], nxt[1], heading, twd),
            )

    if not math.isfinite(best_finish):
        raise RuntimeError(
            f"A* did not reach the finish ({expansions} expansions, window {window:.1f} nm)"
        )
    return Reference(best_finish, 0, 0, "astar"), expansions


def astar_course(
    boat: Boat,
    wind: SynthWind,
    geo: GeoContext,
    points: list[tuple[float, float]],
    radii: list[float],
    depart: datetime,
    *,
    step_nm: float = 0.12,
    cell_nm: float = 0.3,
    heading_step_deg: float = 10.0,
    max_expansions: int = 80_000,
) -> Reference:
    total = 0.0
    expansions = 0
    t_cursor = 0.0
    for i in range(len(points) - 1):
        ref, n = astar_leg(
            boat,
            wind,
            geo,
            points[i],
            points[i + 1],
            radii[i],
            depart,
            t_cursor,
            step_nm=step_nm,
            cell_nm=cell_nm,
            heading_step_deg=heading_step_deg,
            max_expansions=max_expansions,
        )
        total += ref.elapsed_s
        t_cursor += ref.elapsed_s
        expansions += n
    return Reference(total, 0, 0, f"astar:{expansions}")
