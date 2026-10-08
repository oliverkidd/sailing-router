"""Headland waypoint chain: navigable corridor mask → A* → string pull.

The Euclidean shortest path among polygonal obstacles bends only at convex
obstacle vertices (Lozano-Pérez & Wesley 1979), so the bend points of a taut
path *are* the headlands. A full visibility graph is not affordable here: a
Solent land clip is 20k–35k exterior coordinates over 25–125 polygons, and it
would still miss every sandbank, because shoals live in the depth raster rather
than in the land polygons. So the bends are found on the navigable grid (which
covers land and shoal alike) and the resulting polyline is pulled taut.

The chain is **advisory**. It steers the router's heading seed and its
remaining-distance heuristic; it is not a route. Every sailed step is still
validated against GeoContext and arrival is still decided by the real marks, so
a poor chain degrades to the un-guided behaviour rather than producing an
illegal route.
"""

from __future__ import annotations

import heapq
import logging
import math
from dataclasses import dataclass

import numpy as np
import shapely
from scipy import ndimage

from ..navmath import norm360, signed_diff
from ..schemas import LatLon
from . import GeoContext, bearing, destination, distance_nm

logger = logging.getLogger(__name__)

NM_PER_DEG_LAT = 60.0
# A* is pure Python; beyond this the per-leg cost stops being free. Stride down.
MAX_GRID_CELLS = 160_000
# Extra cost for hugging an obstacle, so narrows like Hurst take the channel
# centreline instead of shaving the spit. Must stay >= 0 so the Euclidean
# heuristic remains admissible (multipliers never drop below 1).
CLEARANCE_BIAS = 0.6
# How far out clearance stops mattering, as a multiple of clearance_nm.
CLEARANCE_BIAS_SPAN = 3.0
# The way out of a bay can lie well outside the node→target box (leaving the
# Solent for the island's south coast means going 7 nm west of either fix), so
# one widened retry runs before giving up. MAX_GRID_CELLS still bounds the work.
WIDE_CORRIDOR_FACTOR = 4.0
# Each pass splits every blocked span once, so obstacles clear geometrically.
REFINE_PASSES = 4
# Extra depth (m) above the router's hard min+margin for the advisory chain.
# Without this the A* corridor treats the Bembridge 4.5 m shelf as open water
# (clearance is huge across a wide shoal) and the router aims into the contour
# that then thrashes. Start/goal cells are still forced open below.
WAYPOINT_DEPTH_BUFFER_M = 1.0

_NEIGHBOURS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


@dataclass(slots=True)
class _CorridorGrid:
    lats: np.ndarray  # ascending
    lons: np.ndarray  # ascending
    navigable: np.ndarray  # bool, [nlat, nlon]
    clearance_nm: np.ndarray  # nm to the nearest blocked cell
    nm_per_row: float
    nm_per_col: float
    start_ij: tuple[int, int]
    goal_ij: tuple[int, int]

    def pos(self, ij: tuple[int, int]) -> LatLon:
        return (float(self.lats[ij[0]]), float(self.lons[ij[1]]))

    def cell_of(self, p: LatLon) -> tuple[int, int]:
        i = int(np.clip(np.searchsorted(self.lats, p[0]), 0, self.lats.size - 1))
        j = int(np.clip(np.searchsorted(self.lons, p[1]), 0, self.lons.size - 1))
        return i, j

    def contains(self, p: LatLon) -> bool:
        return bool(
            self.lats[0] <= p[0] <= self.lats[-1] and self.lons[0] <= p[1] <= self.lons[-1]
        )


@dataclass(slots=True)
class WaypointChain:
    """Advisory headland chain, plus the mask it was found on.

    ``visible`` deliberately reuses the corridor mask instead of GeoContext:
    same obstacle set, but a couple of hundred array lookups rather than a few
    hundred prepared-geometry ``covers`` calls. The router tests line of sight
    once per frontier node per step, so a 12 nm ``blocked_segment`` there would
    cost more than the whole expansion it is guiding.
    """

    points: list[LatLon]
    grid: _CorridorGrid | None = None

    def __bool__(self) -> bool:
        return bool(self.points)

    def visible(self, a: LatLon, b: LatLon) -> bool:
        g = self.grid
        if g is None:
            return True
        # Off the corridor the chain no longer describes the boat's situation;
        # reporting visibility retires it and restores un-guided behaviour.
        if not g.contains(a) or not g.contains(b):
            return True
        return _grid_los(g.navigable, g.cell_of(a), g.cell_of(b))


def find_waypoints(
    geo: GeoContext,
    start: LatLon,
    target: LatLon,
    *,
    clearance_nm: float = 0.2,
    corridor_margin_nm: float = 2.0,
    max_points: int = 8,
    grid_stride: int = 1,
) -> WaypointChain:
    """Taut chain of headland waypoints between start and target.

    The chain is empty when the direct segment is already clear (the common
    case), when there is no depth raster to build a mask from, or when no path
    exists — in all three the caller behaves exactly as it did without guidance.
    """
    if not geo.blocked_segment(start, target):
        return WaypointChain([])
    if geo.depth is None:
        return WaypointChain([])

    # Long coastal hops (Messina Strait, Land's End → Fastnet) leave the
    # start→target box by tens of miles. Scale the search box with leg length
    # so a 2 nm Solent margin does not silently return an empty chain.
    leg_nm = distance_nm(start, target)
    base = max(corridor_margin_nm, min(12.0, 0.12 * leg_nm) if leg_nm > 25.0 else corridor_margin_nm)
    margins = (
        base,
        base * WIDE_CORRIDOR_FACTOR,
        max(base * WIDE_CORRIDOR_FACTOR, min(40.0, 0.25 * leg_nm)) if leg_nm > 40.0 else base * WIDE_CORRIDOR_FACTOR,
    )
    # Deduplicate while keeping order.
    seen_m: set[float] = set()
    uniq_margins: list[float] = []
    for m in margins:
        key = round(m, 2)
        if key in seen_m:
            continue
        seen_m.add(key)
        uniq_margins.append(m)

    grid = None
    cells: list[tuple[int, int]] = []
    for margin in uniq_margins:
        grid = _corridor_grid(geo, start, target, margin, grid_stride)
        if grid is None:
            continue
        cells = _astar(grid, clearance_nm)
        if len(cells) >= 3:
            break
    if grid is None or len(cells) < 3:
        return WaypointChain([])

    pulled = _string_pull(grid, cells, _collapse_collinear(cells))
    kept = _refine(geo, grid, cells, pulled, start, target, max_points)
    chain: list[LatLon] = [start, *[grid.pos(cells[k]) for k in kept[1:-1]], target]
    chain = _pull_taut(geo, chain, max_points)

    interior: list[LatLon] = []
    for k in range(1, len(chain) - 1):
        prev = interior[-1] if interior else chain[0]
        original = chain[k]
        moved = _nudge_to_clearance(grid, geo, original, clearance_nm)
        # Offsetting for clearance must not push the bend across something else.
        if moved != original and (
            geo.blocked_segment(prev, moved) or geo.blocked_segment(moved, chain[k + 1])
        ):
            moved = original
        if not geo.blocked_point(moved):
            interior.append(moved)

    interior = _clear_blocked_hops(geo, start, interior, target)

    logger.debug(
        "waypoints %s->%s grid=%sx%s cells=%d chain=%d",
        start,
        target,
        grid.navigable.shape[0],
        grid.navigable.shape[1],
        len(cells),
        len(interior),
    )
    return WaypointChain(interior, grid)


def _corridor_grid(
    geo: GeoContext,
    start: LatLon,
    target: LatLon,
    margin_nm: float,
    stride: int,
) -> _CorridorGrid | None:
    depth = geo.depth
    if depth is None or depth.lats.size < 4 or depth.lons.size < 4:
        return None

    mid_lat = 0.5 * (start[0] + target[0])
    cos_lat = max(0.2, math.cos(math.radians(mid_lat)))
    margin_lat = max(margin_nm, 0.0) / NM_PER_DEG_LAT
    margin_lon = margin_lat / cos_lat

    i0 = _index(depth.lats, min(start[0], target[0]) - margin_lat)
    i1 = _index(depth.lats, max(start[0], target[0]) + margin_lat)
    j0 = _index(depth.lons, min(start[1], target[1]) - margin_lon)
    j1 = _index(depth.lons, max(start[1], target[1]) + margin_lon)
    if i1 - i0 < 3 or j1 - j0 < 3:
        return None

    step = max(1, int(stride))
    while ((i1 - i0) // step + 1) * ((j1 - j0) // step + 1) > MAX_GRID_CELLS:
        step += 1

    lats = np.ascontiguousarray(depth.lats[i0 : i1 + 1 : step], dtype=np.float64)
    lons = np.ascontiguousarray(depth.lons[j0 : j1 + 1 : step], dtype=np.float64)
    if lats.size < 4 or lons.size < 4:
        return None
    d = depth.depth_m[i0 : i1 + 1 : step, j0 : j1 + 1 : step].astype(np.float64)

    # fetch.py already stores land / NaN / nodata as 0.0, so the depth threshold
    # covers shoals *and* most land; the land polygons are the authority for
    # coastline shape, so rasterise them too rather than trusting the raster.
    # Buffer keeps advisory waypoints off barely-legal shelves (see constant).
    threshold = (
        geo.min_depth_m + geo.depth_safety_margin_m + WAYPOINT_DEPTH_BUFFER_M
    )
    navigable = np.isfinite(d) & (d >= threshold)
    lon_mesh, lat_mesh = np.meshgrid(lons, lats)
    if not geo.land.is_empty:
        navigable &= ~shapely.contains_xy(geo.land.geometry, lon_mesh, lat_mesh)
    for poly in geo.exclusions:
        navigable &= ~shapely.contains_xy(poly, lon_mesh, lat_mesh)

    nm_per_row = float(lats[1] - lats[0]) * NM_PER_DEG_LAT
    nm_per_col = float(lons[1] - lons[0]) * NM_PER_DEG_LAT * cos_lat

    grid = _CorridorGrid(
        lats=lats,
        lons=lons,
        navigable=navigable,
        clearance_nm=np.zeros_like(d),
        nm_per_row=abs(nm_per_row),
        nm_per_col=abs(nm_per_col),
        start_ij=(0, 0),
        goal_ij=(0, 0),
    )
    # Treat the crop boundary as an obstacle. Without this the edge cells have
    # no blocked neighbour beyond them, so the clearance transform reports open
    # water off the edge of the world and both the A* bias and the offset pull
    # waypoints out towards it.
    navigable[0, :] = False
    navigable[-1, :] = False
    navigable[:, 0] = False
    navigable[:, -1] = False

    grid.start_ij = grid.cell_of(start)
    grid.goal_ij = grid.cell_of(target)
    # The endpoints are real fixes the router already accepts; the resampled
    # cell under them can still fail the threshold (conservative min-of-2×2 in
    # DepthField vs a single stride sample here), so open them up explicitly.
    for i, j in (grid.start_ij, grid.goal_ij):
        navigable[max(0, i - 1) : i + 2, max(0, j - 1) : j + 2] = True

    grid.clearance_nm = ndimage.distance_transform_edt(
        navigable, sampling=(grid.nm_per_row, grid.nm_per_col)
    )
    return grid


def _index(axis: np.ndarray, value: float) -> int:
    return int(np.clip(np.searchsorted(axis, value), 0, axis.size - 1))


def _astar(grid: _CorridorGrid, clearance_nm: float) -> list[tuple[int, int]]:
    """8-connected A* in nm, biased away from obstacles. [] when unreachable."""
    nav = grid.navigable
    nrow, ncol = nav.shape
    si, sj = grid.start_ij
    gi, gj = grid.goal_ij
    if not nav[si, sj] or not nav[gi, gj]:
        return []

    span = max(clearance_nm * CLEARANCE_BIAS_SPAN, 1e-6)
    mult = 1.0 + CLEARANCE_BIAS * np.clip(1.0 - grid.clearance_nm / span, 0.0, 1.0)
    mult_flat = mult.ravel().tolist()
    nav_flat = nav.ravel().tolist()

    row_nm = grid.nm_per_row
    col_nm = grid.nm_per_col
    step_nm = {d: math.hypot(d[0] * row_nm, d[1] * col_nm) for d in _NEIGHBOURS}

    def heuristic(i: int, j: int) -> float:
        return math.hypot((i - gi) * row_nm, (j - gj) * col_nm)

    start_f = si * ncol + sj
    goal_f = gi * ncol + gj
    g = [math.inf] * (nrow * ncol)
    parent = [-1] * (nrow * ncol)
    closed = bytearray(nrow * ncol)
    g[start_f] = 0.0
    heap: list[tuple[float, int]] = [(heuristic(si, sj), start_f)]

    while heap:
        _, cur = heapq.heappop(heap)
        if closed[cur]:
            continue
        closed[cur] = 1
        if cur == goal_f:
            break
        ci, cj = divmod(cur, ncol)
        gc = g[cur]
        for di, dj in _NEIGHBOURS:
            ni = ci + di
            nj = cj + dj
            if ni < 0 or nj < 0 or ni >= nrow or nj >= ncol:
                continue
            nf = ni * ncol + nj
            if closed[nf] or not nav_flat[nf]:
                continue
            cost = gc + step_nm[(di, dj)] * mult_flat[nf]
            if cost < g[nf]:
                g[nf] = cost
                parent[nf] = cur
                heapq.heappush(heap, (cost + heuristic(ni, nj), nf))

    if not closed[goal_f]:
        return []
    out: list[tuple[int, int]] = []
    node = goal_f
    while node != -1:
        out.append(divmod(node, ncol))
        node = parent[node]
    out.reverse()
    return out


def _collapse_collinear(cells: list[tuple[int, int]]) -> list[int]:
    """Indices of the cells where the grid step direction changes."""
    if len(cells) < 3:
        return list(range(len(cells)))
    out = [0]
    for k in range(1, len(cells) - 1):
        prev = cells[out[-1]]
        cur = cells[k]
        nxt = cells[k + 1]
        if (cur[0] - prev[0], cur[1] - prev[1]) != (nxt[0] - cur[0], nxt[1] - cur[1]):
            out.append(k)
    out.append(len(cells) - 1)
    return out


def _grid_los(nav: np.ndarray, a: tuple[int, int], b: tuple[int, int]) -> bool:
    """True when every cell sampled along a→b is navigable."""
    di = b[0] - a[0]
    dj = b[1] - a[1]
    n = max(abs(di), abs(dj))
    if n == 0:
        return bool(nav[a[0], a[1]])
    for k in range(n + 1):
        f = k / n
        if not nav[a[0] + int(round(di * f)), a[1] + int(round(dj * f))]:
            return False
    return True


def _string_pull(
    grid: _CorridorGrid,
    cells: list[tuple[int, int]],
    bends: list[int],
) -> list[int]:
    """Simple funnel: keep advancing while the next bend is still in sight.

    Returns indices into ``cells`` (not into ``bends``) so ``_refine`` can split
    anywhere along the raw path. Line of sight is tested on the mask rather than
    via GeoContext — same obstacle set, but ~200 array lookups instead of
    hundreds of shapely ``covers`` calls per test.
    """
    n = len(bends)
    if n < 3:
        return list(bends)
    nav = grid.navigable
    out = [bends[0]]
    i = 0
    while i < n - 1:
        j = i + 1
        while j + 1 < n and _grid_los(nav, cells[bends[i]], cells[bends[j + 1]]):
            j += 1
        out.append(bends[j])
        i = j
    return out


def _refine(
    geo: GeoContext,
    grid: _CorridorGrid,
    cells: list[tuple[int, int]],
    kept: list[int],
    start: LatLon,
    target: LatLon,
    max_points: int,
) -> list[int]:
    """Re-insert dropped path cells wherever GeoContext still sees an obstacle.

    The funnel pass runs on the strided mask, so it can shortcut across a thin
    ledge that the 0.05 nm ``blocked_segment`` sampling picks up. Each pass
    splits the *longest* offending spans first: a heuristic pointing the wrong
    way over 12 nm costs far more than one clipping a rock over 0.2 nm, and
    depth-first bisection of the first blocked span would spend the whole
    waypoint budget near the start.
    """
    out = list(kept)
    cell_nm = max(grid.nm_per_row, grid.nm_per_col)

    for _ in range(REFINE_PASSES):
        budget = max_points - (len(out) - 2)
        if budget <= 0:
            break

        def pos(k: int, chain: list[int] = out) -> LatLon:
            if k == 0:
                return start
            if k == len(chain) - 1:
                return target
            return grid.pos(cells[chain[k]])

        blocked: list[tuple[float, int]] = []
        for k in range(len(out) - 1):
            lo, hi = out[k], out[k + 1]
            if hi - lo < 2:
                continue
            a, b = pos(k), pos(k + 1)
            span = distance_nm(a, b)
            # Shorter than a couple of depth samples, a midpoint cannot get
            # around the obstacle the mask and the segment test disagree on.
            if span < max(0.2, cell_nm):
                continue
            if geo.blocked_segment(a, b):
                blocked.append((span, k))
        if not blocked:
            break

        blocked.sort(reverse=True)
        for shift, k in enumerate(sorted(k for _, k in blocked[:budget])):
            at = k + shift
            out.insert(at + 1, (out[at] + out[at + 1]) // 2)
    return out


def _clear_blocked_hops(
    geo: GeoContext,
    start: LatLon,
    interior: list[LatLon],
    target: LatLon,
) -> list[LatLon]:
    """Insert a point wherever a chain hop crosses land or shoal.

    A 0.5 nm hop can clip the Needles while both ends sit in deep water. The
    boat then aims at the far point, hits the rock, and escapes back the way
    it came. Offsets are tried on both sides of the hop until both new legs
    are clear.
    """
    out = [start, *interior, target]
    for _ in range(6):
        blocked_at = None
        for k in range(len(out) - 1):
            if distance_nm(out[k], out[k + 1]) < 0.2:
                continue
            if geo.blocked_segment(out[k], out[k + 1]):
                blocked_at = k
                break
        if blocked_at is None:
            break
        a, b = out[blocked_at], out[blocked_at + 1]
        mid = ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)
        brg = bearing(a, b)
        inserted = None
        for dist in (0.4, 0.8, 1.2, 2.0, 3.0):
            for side in (90.0, -90.0):
                cand = destination(mid, norm360(brg + side), dist)
                if geo.blocked_point(cand):
                    continue
                if geo.blocked_segment(a, cand) or geo.blocked_segment(cand, b):
                    continue
                inserted = cand
                break
            if inserted is not None:
                break
        if inserted is None:
            break
        out.insert(blocked_at + 1, inserted)
    return out[1:-1]


def _turn_deg(a: LatLon, p: LatLon, b: LatLon) -> float:
    return abs(signed_diff(bearing(p, b), bearing(a, p)))


def _pull_taut(geo: GeoContext, chain: list[LatLon], max_points: int) -> list[LatLon]:
    """Drop every bend GeoContext says we can now see past; then trim to the cap.

    The funnel pass ran on the strided mask and ``_refine`` then re-inserted raw
    A* cells wherever the finer segment sampling disagreed, so the surviving
    chain carries the clearance-biased detours of the grid path. Shortcutting
    against ``blocked_segment`` — the same test the router itself sails by — is
    what makes the chain taut rather than merely legal, and it subsumes the
    8-connected staircase noise for free.
    """
    out = list(chain)
    cap = max(0, int(max_points))
    while len(out) > 2:
        over_cap = len(out) - 2 > cap
        flattest_first = sorted(
            range(1, len(out) - 1),
            key=lambda k: _turn_deg(out[k - 1], out[k], out[k + 1]),
        )
        popped = False
        for k in flattest_first:
            if over_cap or not geo.blocked_segment(out[k - 1], out[k + 1]):
                out.pop(k)
                popped = True
                break
        if not popped:
            break
    return out


def _nudge_to_clearance(
    grid: _CorridorGrid,
    geo: GeoContext,
    p: LatLon,
    clearance_nm: float,
) -> LatLon:
    """Push a bend away from the nearest obstacle, up to clearance_nm."""
    if clearance_nm <= 0:
        return p
    i, j = grid.cell_of(p)
    clear = grid.clearance_nm
    here = float(clear[i, j])
    if here >= clearance_nm:
        return p
    nrow, ncol = clear.shape
    # Steepest ascent on the clearance field is, by construction, straight away
    # from whatever is nearest — no need to reason about which side the land is.
    d_row = float(clear[min(i + 1, nrow - 1), j] - clear[max(i - 1, 0), j])
    d_col = float(clear[i, min(j + 1, ncol - 1)] - clear[i, max(j - 1, 0)])
    if abs(d_row) < 1e-9 and abs(d_col) < 1e-9:
        return p
    brg = math.degrees(math.atan2(d_col * grid.nm_per_col, d_row * grid.nm_per_row)) % 360.0
    for frac in (1.0, 0.6, 0.3):
        candidate = destination(p, brg, clearance_nm * frac)
        ci, cj = grid.cell_of(candidate)
        # A full clearance_nm step overshoots anywhere narrower than that (a
        # 0.34 nm channel, say), so only keep a move that actually improves.
        if float(clear[ci, cj]) > here and not geo.blocked_point(candidate):
            return candidate
    return p
