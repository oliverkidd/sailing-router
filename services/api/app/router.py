"""Isochrone router: stepwise mark-bearing optimisation.

Pipeline per leg:
  1. Seed = mark bearing, or closer upwind/downwind VMG if mark is in no-go.
  2. Progressive fan-out (±heading_step) until a land/depth-clear heading exists.
  3. Commit that heading until mark bearing clears, layline, wind change, or geo hit.
  4. Prune frontier by ETA proxy; backtrack to RouteLegs.

Depth is a hard min+margin cutoff only (no soft shore cost). Tack/gybe use boat
penalties; small course changes use fractions of tack_penalty_s scaled by TWS.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .boat import Boat
from .geo import GeoContext, bearing, destination, distance_nm
from .geo.waypoints import WaypointChain, find_waypoints
from .metocean import WindField
from .navmath import magnitude_bearing, norm360, signed_diff, twa_from, vector
from .schemas import Course, Mark, ModelRoute, RouteLeg

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None]

# Guide advance is a function of position and index only, so it caches by cell.
# ~0.24 nm — fine enough that the answer is stable across the cell, coarse
# enough to collapse a few thousand frontier nodes onto a few hundred keys.
GUIDE_CACHE_CELL_DEG = 0.004


@dataclass(slots=True)
class Node:
    lat: float
    lon: float
    t: datetime
    parent: Node | None = None
    heading: float | None = None
    bsp: float = 0.0
    tws: float = 0.0
    twd: float = 0.0
    twa: float = 0.0
    sog: float = 0.0
    cog: float = 0.0
    maneuver: str = "none"
    n_maneuvers: int = 0
    depth_m: float = 0.0
    hazard_cost: float = 0.0  # always 0; kept for RouteLeg schema compat
    rethink: bool = False  # next expand re-seeds from mark bearing
    guide_idx: int = 0  # index into the leg's waypoint chain; never goes back
    # Memo for _guide within one expand (pos + guide_idx → point).
    _guide_memo: tuple[float, float, int, float, float] | None = None

    @property
    def pos(self) -> tuple[float, float]:
        return (self.lat, self.lon)


@dataclass
class RouterParams:
    time_step_s: int = 300
    heading_step_deg: float = 5.0
    sector_width_deg: float = 5.0
    cluster_cell_deg: float = 0.03
    max_frontier: int = 3000
    max_steps_per_leg: int = 4000
    maneuver_prune_nm: float = 0.55
    depth_safety_margin_m: float = 1.0
    no_progress_steps: int = 60
    max_consecutive_maneuvers: int = 16
    maneuver_overflow_nm: float = 1.5
    min_bsp_fraction_of_beat: float = 0.4
    max_commit_s: float = 3600.0
    wind_delta_twd_deg: float = 8.0
    wind_delta_tws_kn: float = 1.5
    mark_probe_nm: float = 1.0
    near_mark_nm: float = 0.8
    # Fan / seeding
    fan_half_max_deg: float = 180.0
    fan_probe_nm_max: float = 0.25
    fan_probe_nm_min: float = 0.15
    fan_probe_mark_frac: float = 0.25
    same_heading_bonus_s: float = 5.0
    same_heading_band_deg: float = 2.5
    vmg_hysteresis_deg: float = 25.0
    # Course-change penalties
    course_change_free_deg: float = 10.0
    course_change_moderate_deg: float = 20.0
    course_change_tws_scale_kn: float = 20.0
    course_change_heavy_frac_small: float = 0.25
    course_change_heavy_frac_large: float = 0.5
    course_change_mild_frac: float = 0.25
    # Tack / gybe offering
    downwind_twa_gate_deg: float = 90.0
    heading_ref_max_err_deg: float = 90.0
    opposite_maneuver_course_err_deg: float = 90.0
    opposite_tack_twa_slack_deg: float = 5.0
    opposite_gybe_twa_slack_deg: float = 5.0
    opposite_maneuver_heading_spread_deg: float = 45.0
    # Min distance between tacks/gybes unless the held board is into shore.
    # 1 nm was short enough for Solent forecast beats to flip every board.
    tack_lockout_nm: float = 2.0
    tack_shore_probe_nm: float = 0.8
    # Look-ahead: how far (nm) to compare held vs opposite board sea-room.
    # Oversail = this board runs out of deep water first while the other still
    # has channel — not "never sail shallow" (both boards short → allow).
    board_room_lookout_nm: float = 2.0
    # Depth (m) treated as the edge of sea-room when we are still in deep water.
    # Once already shallower than this, only the hard geo block counts.
    board_room_depth_m: float = 6.0
    # Opposite board must beat held clear-water by at least this (nm) to count
    # as the longer board (filters parallel channel shores).
    board_room_margin_nm: float = 0.3
    # Discard headings that increase chain-rem (sailing away from the aim)
    # when the mark is not blocked — stops the eastbound spike on a west beat.
    progress_probe_nm: float = 0.8
    progress_rem_slack_nm: float = 0.2
    # Rethink / layline
    rethink_heading_vs_bearing_deg: float = 10.0
    layline_stop_deg: float = 35.0
    past_layline_deg: float = 90.0
    # Stall / commit
    light_air_twd_tws_kn: float = 2.0
    prune_floor_bsp_kn: float = 0.5
    soft_cost_s_per_nm: float = 600.0  # 3600/6
    wind_epoch_commit_frac: float = 0.5
    commit_arrival_buffer_s: float = 60.0
    stall_rem_factor: float = 4.0
    stall_rem_offset: int = 40
    geo_hit_retry_fraction: float = 0.5
    short_commit_min_s: float = 60.0
    creep_commit_min_s: float = 30.0
    # Arrival
    finish_snap_radius_nm: float = 0.2
    mark_arrival_break_nm: float = 0.05
    # Headland chain. Remaining distance follows the chain, so a boat can sail
    # away from a mark to get around land without the prune calling that a stall.
    waypoint_guidance: int = 1
    waypoint_clearance_nm: float = 0.2
    waypoint_corridor_margin_nm: float = 5.0
    waypoint_max_points: int = 8
    waypoint_grid_stride: int = 1
    waypoint_pass_nm: float = 0.3


def _wind_side(heading: float, twd: float) -> float:
    d = signed_diff(twd, heading)
    if abs(d) < 1e-6 or abs(abs(d) - 180.0) < 1e-6:
        return 0.0
    return 1.0 if d > 0 else -1.0


def _detect_maneuver(parent: Node, heading: float, twd: float, twa: float) -> str:
    if parent.heading is None:
        return "none"
    if _wind_side(parent.heading, parent.twd) == _wind_side(heading, parent.twd):
        return "none"
    return "tack" if twa < 90.0 else "gybe"


def _course_change_penalty_s(
    delta_deg: float, tws_kn: float, tack_penalty_s: float, p: RouterParams
) -> float:
    """Same-tack course-change cost: fractions of tack_penalty_s, scaled by TWS."""
    d = abs(delta_deg)
    if d < p.course_change_free_deg:
        return 0.0
    scale_kn = p.course_change_tws_scale_kn
    if d <= p.course_change_moderate_deg and tws_kn < scale_kn:
        return 0.0
    if tws_kn >= scale_kn:
        scale = max(1.0, tws_kn / scale_kn)
        frac = (
            p.course_change_heavy_frac_small
            if d <= p.course_change_moderate_deg
            else p.course_change_heavy_frac_large
        )
        return min(tack_penalty_s, frac * tack_penalty_s * scale)
    return min(tack_penalty_s, p.course_change_mild_frac * tack_penalty_s)


def _remaining_nm(node: Node, target: tuple[float, float]) -> float:
    return distance_nm(node.pos, target)


def _prune_score(
    node: Node,
    rem: float,
    maneuver_nm: float,
    max_maneuvers: int,
    overflow_nm: float,
    p: RouterParams,
) -> float:
    """Lower is better: ETA proxy (time + rem/bsp) + soft maneuver costs."""
    bsp = max(node.bsp, p.prune_floor_bsp_kn)
    eta_s = node.t.timestamp() + (rem / bsp) * 3600.0
    overflow = max(0, node.n_maneuvers - max_maneuvers)
    soft_s = (maneuver_nm * node.n_maneuvers + overflow_nm * overflow) * p.soft_cost_s_per_nm
    same_bonus = 0.0
    if (
        node.parent is not None
        and node.parent.heading is not None
        and node.heading is not None
        and abs(signed_diff(node.heading, node.parent.heading)) < p.same_heading_band_deg
        and node.maneuver == "none"
    ):
        same_bonus = p.same_heading_bonus_s
    return eta_s + soft_s - same_bonus


def _advance(lat: float, lon: float, gx: float, gy: float, dt_s: float) -> tuple[float, float]:
    speed, brg = magnitude_bearing(gx, gy)
    dist = speed * (dt_s / 3600.0)
    if dist <= 0:
        return lat, lon
    return destination((lat, lon), brg, dist)


def _seconds_until_next_wind_epoch(wind: Any, t: datetime) -> float | None:
    """Seconds until the next wind time sample after t, if any."""
    times = getattr(wind, "times", None)
    if times is None:
        return None
    try:
        n = len(times)
    except TypeError:
        return None
    if n < 2:
        return None
    te = t.timestamp()
    for i in range(n):
        ti = float(times[i])
        if ti > te + 1.0:
            return ti - te
    return None


def _seconds_of_wind_remaining(wind: Any, t: datetime) -> float | None:
    """Seconds of forecast left after t (until the last wind sample).

    When the boat sits on the last epoch label, there is no *next* epoch, but
    wind_at still works until that last timestamp — commits must not step past it
    or they abort with elapsed=0 and look like frontier_empty.
    """
    times = getattr(wind, "times", None)
    if times is None:
        return None
    try:
        n = len(times)
    except TypeError:
        return None
    if n < 1:
        return None
    return float(times[-1]) - t.timestamp()


@dataclass
class _PauseState:
    start_node: Node
    last_arrival: Node
    frontier: list[Node]
    leg_index: int
    targets: list[Mark]
    leg_warnings: list[str]


class IsochroneRouter:
    def __init__(
        self,
        boat: Boat,
        wind: WindField,
        geo: GeoContext,
        params: RouterParams,
        on_progress: ProgressCallback | None = None,
        model_id: str = "",
    ):
        self.boat = boat
        self.wind = wind
        self.geo = geo
        self.p = params
        self.on_progress = on_progress
        self.model_id = model_id
        self.geo.depth_safety_margin_m = params.depth_safety_margin_m
        self._leg_warnings: list[str] = []
        self._unreachable = False
        self._pause: _PauseState | None = None
        # Leg-scoped waypoint guidance, rebuilt by _begin_guidance.
        self._chain: WaypointChain | None = None
        self._guide_pts: list[tuple[float, float]] = []
        self._guide_suffix_nm: list[float] = []
        self._guide_cache: dict[tuple[int, int, int], int] = {}
        self._route_waypoints: list[tuple[float, float]] = []
        # 0 = opportunistic short probes (fast); bumps toward careful when the
        # frontier thrashes or geo-hits spike. Resets each leg.
        self._careful: int = 0
        self._geo_hit_streak: int = 0

    @property
    def can_resume(self) -> bool:
        return self._pause is not None

    def _escalate_careful(self, reason: str) -> None:
        """Move from fast probes toward longer / denser geo checks."""
        prev = self._careful
        self._careful = min(2, self._careful + 1)
        if self._careful != prev:
            # Tighten segment sampling so shore pockets stop looking navigable.
            if self._careful == 1:
                self.geo.segment_sample_nm = min(float(self.geo.segment_sample_nm), 0.03)
            elif self._careful >= 2:
                self.geo.segment_sample_nm = min(float(self.geo.segment_sample_nm), 0.02)
            logger.info(
                "probe careful %d→%d (%s) model=%s sample=%.3fnm",
                prev,
                self._careful,
                reason,
                self.model_id or "-",
                self.geo.segment_sample_nm,
            )

    def _aim_depth_step_nm(self) -> float:
        """Depth sample spacing along mark/guide chords (nm)."""
        # Fast: 1.5 nm. Medium: 0.5 nm. Careful: chart-fine via blocked_segment.
        return (1.5, 0.5, 0.05)[self._careful]

    def _effective_probe_nm(self, requested_nm: float) -> float:
        """Opportunistic short probe; stretch when careful after thrash/geo hits."""
        scale = (1.0, 2.0, 3.5)[self._careful]
        return max(requested_nm, min(requested_nm * scale, 6.0))

    def _mark_line_blocked(
        self, origin: tuple[float, float], target: tuple[float, float]
    ) -> bool:
        """True if the rhumb to ``target`` crosses land or shallows.

        Opportunistic: shapely land chord (cheap) + sparse depth. After thrash,
        fall back to full ``blocked_segment`` (pre-perf quality).
        """
        if self._careful >= 2:
            return self.geo.blocked_segment(origin, target)
        if self.geo.land.segment_crosses_land(origin, target):
            return True
        if self.geo.depth is None:
            return False
        dist = distance_nm(origin, target)
        if dist <= 0:
            return self.geo.too_shallow(origin)
        step = self._aim_depth_step_nm()
        n = max(1, int(math.ceil(dist / step)))
        for i in range(1, n + 1):
            f = min(1.0, i / n)
            lat = origin[0] + f * (target[0] - origin[0])
            lon = origin[1] + f * (target[1] - origin[1])
            if self.geo.too_shallow((lat, lon)):
                return True
        return False

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_progress is not None:
            self.on_progress(event)

    def _begin_guidance(
        self, origin: tuple[float, float], tgt: tuple[float, float]
    ) -> None:
        """Find this leg's headland chain, once, before the frontier expands."""
        self._chain = None
        self._guide_pts = [tgt]
        self._guide_suffix_nm = [0.0]
        self._guide_cache = {}
        if not self.p.waypoint_guidance:
            return
        chain = find_waypoints(
            self.geo,
            origin,
            tgt,
            clearance_nm=self.p.waypoint_clearance_nm,
            corridor_margin_nm=self.p.waypoint_corridor_margin_nm,
            max_points=max(1, int(self.p.waypoint_max_points)),
            grid_stride=max(1, int(self.p.waypoint_grid_stride)),
        )
        if not chain:
            return
        self._chain = chain
        self._guide_pts = [*chain.points, tgt]
        # suffix[k] = distance from guide k to the mark along the rest of the chain.
        suffix = [0.0] * len(self._guide_pts)
        for k in range(len(self._guide_pts) - 2, -1, -1):
            suffix[k] = suffix[k + 1] + distance_nm(
                self._guide_pts[k], self._guide_pts[k + 1]
            )
        self._guide_suffix_nm = suffix
        self._route_waypoints.extend(chain.points)
        logger.info(
            "waypoint guidance: %d points, %.2f nm along vs %.2f nm direct",
            len(chain.points),
            suffix[0] + distance_nm(origin, self._guide_pts[0]),
            distance_nm(origin, tgt),
        )

    def _advance_guide(self, pos: tuple[float, float], idx: int) -> int:
        """Index of the first chain point neither in sight nor already rounded.

        Line of sight is the primary rule (Theta*-style shortcut): it lets a boat
        passing 2 nm outside a headland skip that waypoint instead of being
        dragged back in to it, and once the mark itself comes into sight the
        index reaches the end and the chain stops having any effect.

        Proximity is the necessary backstop. The chain is found on a strided
        mask, so around something as thin as the Needles two consecutive points
        can be navigable while the chord between them is not — and with sight
        alone the boat would sit on the waypoint it has already rounded forever,
        because remaining distance cannot fall below that point's chain suffix.
        """
        chain = self._chain
        last = len(self._guide_pts) - 1
        if chain is None or idx >= last:
            return last
        key = (
            int(pos[0] / GUIDE_CACHE_CELL_DEG),
            int(pos[1] / GUIDE_CACHE_CELL_DEG),
            idx,
        )
        hit = self._guide_cache.get(key)
        if hit is not None:
            return hit
        out = idx
        pass_nm = self.p.waypoint_pass_nm
        while out < last and (
            self._chain_visible(pos, self._guide_pts[out + 1])
            or distance_nm(pos, self._guide_pts[out]) <= pass_nm
        ):
            out += 1
        self._guide_cache[key] = out
        return out

    def _chain_visible(self, pos: tuple[float, float], point: tuple[float, float]) -> bool:
        """True when the sailed chart, not the coarse corridor mask, can see ``point``.

        The mask misses thin water and invents blocked cells, so a boat steered by
        it aims at a waypoint it could already see past. That is the kink in open
        water. Off the corridor the chain no longer applies and the mark is the aim.
        """
        chain = self._chain
        if chain is None:
            return True
        grid = chain.grid
        if grid is not None and (not grid.contains(pos) or not grid.contains(point)):
            return True
        return not (
            self.geo.blocked_point(point) or self.geo.blocked_segment(pos, point)
        )

    def _guide(self, node: Node) -> tuple[float, float]:
        """Active guidance point for a node, memoised onto it.

        When the water just past this corner is already open, aim there instead
        of at the corner itself. Sailing to the vertex and then snapping onto
        the next leg is the kink on the chart.
        """
        memo = node._guide_memo
        if (
            memo is not None
            and memo[2] == node.guide_idx
            and memo[3] == node.lat
            and memo[4] == node.lon
        ):
            return (memo[0], memo[1])
        last = len(self._guide_pts) - 1
        mark = self._guide_pts[last]
        # Inside 2 nm of the race mark the chain is done — false depth holes
        # around a rock must not keep the boat aimed at a prior waypoint.
        if distance_nm(node.pos, mark) < 2.0:
            node.guide_idx = last
            node._guide_memo = (mark[0], mark[1], node.guide_idx, node.lat, node.lon)
            return mark
        node.guide_idx = self._advance_guide(node.pos, node.guide_idx)
        # Wrong side of a headland: children inherit a guide_idx rounded while
        # the parent was north of the spit, then drift south. Aiming at the
        # next bend drives rem through land and the frontier thrashes. Walk
        # back while the active guide is blocked and the prior bend is still
        # reachable — but not while we are still inside the pass radius of
        # that bend (Needles proximity advance must stick there).
        while node.guide_idx > 0:
            pt = self._guide_pts[node.guide_idx]
            if not (
                self.geo.blocked_point(pt)
                or self.geo.blocked_segment(node.pos, pt)
            ):
                break
            prev = self._guide_pts[node.guide_idx - 1]
            if self.geo.blocked_point(prev) or self.geo.blocked_segment(
                node.pos, prev
            ):
                break
            if distance_nm(node.pos, prev) <= self.p.waypoint_pass_nm:
                break
            node.guide_idx -= 1
        pt = self._guide_pts[node.guide_idx]
        if node.guide_idx >= last:
            node._guide_memo = (pt[0], pt[1], node.guide_idx, node.lat, node.lon)
            return pt
        nxt = self._guide_pts[node.guide_idx + 1]
        # Already inside the pass radius of this corner with the next bend in
        # sight: aim there now. Otherwise we sail into the vertex on a low
        # bearing and snap up when the mark pops clear (Needles underlay kink).
        if (
            distance_nm(node.pos, pt) <= self.p.waypoint_pass_nm
            and self._chain_visible(node.pos, nxt)
        ):
            node.guide_idx = self._advance_guide(node.pos, node.guide_idx)
            pt = self._guide_pts[node.guide_idx]
            if node.guide_idx >= last:
                node._guide_memo = (
                    pt[0],
                    pt[1],
                    node.guide_idx,
                    node.lat,
                    node.lon,
                )
                return pt
            nxt = self._guide_pts[node.guide_idx + 1]
        brg = bearing(pt, nxt)
        for dist in (1.2, 0.6, 0.3, 0.15):
            ahead = destination(pt, brg, dist)
            if self._chain_visible(node.pos, ahead):
                node._guide_memo = (
                    ahead[0],
                    ahead[1],
                    node.guide_idx,
                    node.lat,
                    node.lon,
                )
                return ahead
        node._guide_memo = (pt[0], pt[1], node.guide_idx, node.lat, node.lon)
        return pt

    def _rem(self, node: Node, tgt: tuple[float, float]) -> float:
        """Remaining distance to the mark, around the chain when one is active.

        Straight-line distance is the whole bay-pinging bug: with land between
        node and mark, sailing *around* the obstruction increases it, so the
        prune drops the nodes making real progress and the stall counter fires.
        """
        if self._chain is None:
            return _remaining_nm(node, tgt)
        guide = self._guide(node)
        return distance_nm(node.pos, guide) + self._guide_suffix_nm[node.guide_idx]

    def _ban_aim_bearing(
        self, node: Node, target: tuple[float, float]
    ) -> float:
        """Bearing used by hard anti-spike bans.

        Prefer the guide when it agrees with the mark. When the boat has
        overshot onto the wrong side of a headland waypoint, the guide can
        point reverse of the race mark — banning against that guide then
        rejects the only headings that make progress (Solent westbound died).

        Conversely, when the mark line is through land and the guide is the
        coastal bend (east of IoW on M2→M3), banning against the mark aims
        into the island and strips the only open headings.
        """
        guide = self._guide(node) if self._guide_pts else target
        brg_m = bearing(node.pos, target)
        brg_g = bearing(node.pos, guide)
        if abs(signed_diff(brg_g, brg_m)) > 60.0:
            # Adaptive: fast sparse depth, escalate to full blocked_segment after
            # thrash. A fixed 1.5 nm probe alone lied near Bembridge.
            if self._mark_line_blocked(node.pos, target):
                return brg_g
            return brg_m
        return brg_g

    def _probe_clear(
        self,
        pos: tuple[float, float],
        heading: float,
        *,
        probe_nm: float,
        tws: float | None = None,
        twd: float | None = None,
        stretch: bool = True,
    ) -> bool:
        """True if a short step on heading (with leeway if known) is navigable.

        ``probe_nm`` is the caller's opportunistic length; when ``_careful`` has
        been raised after thrash/geo-hit streaks we stretch it so shore pockets
        that looked clear at 0.8–1.5 nm are rejected before we commit into them.
        Pass ``stretch=False`` for exact look-ahead binary search.
        """
        if stretch:
            probe_nm = self._effective_probe_nm(probe_nm)
        if probe_nm <= 0:
            return True
        course = heading
        if tws is not None and twd is not None:
            twa = twa_from(heading, twd)
            bsp = self.boat.bsp(tws, twa)
            if bsp > 0:
                course = norm360(
                    heading + self.boat.leeway_offset(tws, bsp, twd, heading)
                )
        probe = destination(pos, course, probe_nm)
        # Stranded on a blocked cell (depth pocket): blocked_segment samples the
        # start point, so every heading looks blocked and the frontier dies
        # (Bembridge shelf on M2→M3 @315). Only the path ahead matters then.
        return not self._segment_blocked_ahead(pos, probe)

    def _opposite_upwind(self, heading: float, twd: float, tws: float) -> float:
        """The other upwind VMG board relative to ``heading``."""
        up = self.boat.target_upwind_twa(tws)
        port = norm360(twd + up)
        stbd = norm360(twd - up)
        if abs(signed_diff(heading, port)) <= abs(signed_diff(heading, stbd)):
            return stbd
        return port

    def _board_room_nm(
        self,
        pos: tuple[float, float],
        heading: float,
        *,
        tws: float,
        twd: float,
        max_nm: float,
    ) -> float:
        """Navigable sea-room (nm) along ``heading``, capped at ``max_nm``.

        From deep water, room ends at ``board_room_depth_m`` (the 6 m shelf),
        so we tack before riding the 4–5 m contour. Already on that shelf, room
        ends only at the hard geo block — shallow boards stay legal.
        """
        if max_nm <= 0:
            return 0.0
        depth = self.geo.depth
        shelf = float(self.p.board_room_depth_m)
        d0 = float(depth.depth(pos)) if depth is not None else shelf + 1.0
        use_shelf = depth is not None and d0 >= shelf

        def ok(dist: float) -> bool:
            if dist <= 1e-6:
                return True
            if not self._probe_clear(
                pos, heading, probe_nm=dist, tws=tws, twd=twd, stretch=False
            ):
                return False
            if use_shelf:
                sample = destination(pos, heading, dist)
                if float(depth.depth(sample)) < shelf:
                    return False
            return True

        if ok(max_nm):
            return max_nm
        lo, hi = 0.0, max_nm
        for _ in range(10):
            mid = (lo + hi) * 0.5
            if ok(mid):
                lo = mid
            else:
                hi = mid
        return lo

    def _held_board_deadends_sooner(
        self,
        pos: tuple[float, float],
        heading: float,
        tgt: tuple[float, float],
        tws: float,
        twd: float,
        arrive_nm: float,
    ) -> bool:
        """True when this upwind board runs out of sea-room before the other.

        Gosport oversails: north board hits the 5 m shelf while the south board
        still has longer clear water. Tack onto the longer board.

        Not a deep-water-only policy: when both boards are short (banks on both
        sides, already on a shelf), returns False so shallow attacks stay
        allowed. Also False when this board fetches the mark.
        """
        if distance_nm(pos, tgt) < 1.2:
            return False
        if twa_from(heading, twd) >= 90.0:
            return False
        if self._heading_fetches(pos, heading, tgt, max(arrive_nm, 0.25)):
            return False
        look = float(self.p.board_room_lookout_nm)
        margin = float(self.p.board_room_margin_nm)
        held_room = self._board_room_nm(
            pos, heading, tws=tws, twd=twd, max_nm=look
        )
        if held_room >= look - 1e-3:
            return False
        opp = self._opposite_upwind(heading, twd, tws)
        if not self._probe_clear(
            pos, opp, probe_nm=0.4, tws=tws, twd=twd, stretch=False
        ):
            return False
        end = destination(pos, opp, 0.4)
        if distance_nm(end, tgt) > distance_nm(pos, tgt) - 0.05:
            return False
        opp_room = self._board_room_nm(pos, opp, tws=tws, twd=twd, max_nm=look)
        return opp_room >= held_room + margin

    def _heading_fetches(
        self,
        pos: tuple[float, float],
        heading: float,
        mark: tuple[float, float],
        radius_nm: float,
    ) -> bool:
        """True if holding this heading enters the mark circle on clear water.

        In steady wind the other board is worth taking only once it does.
        Tacking earlier, then tacking back, is the extra-tack loss. A geometric
        fetch through a shoal is not a layline — that was the Needles scribble.
        """
        if radius_nm <= 0:
            radius_nm = self.p.mark_arrival_break_nm
        if distance_nm(pos, mark) <= radius_nm:
            return True
        north = (mark[0] - pos[0]) * 60.0
        east = (mark[1] - pos[1]) * 60.0 * max(0.2, math.cos(math.radians(pos[0])))
        rad = math.radians(heading)
        ux, uy = math.sin(rad), math.cos(rad)
        along = east * ux + north * uy
        miss2 = east * east + north * north - along * along
        if miss2 > radius_nm * radius_nm:
            return False
        enter = along - math.sqrt(max(0.0, radius_nm * radius_nm - miss2))
        if enter < -1e-3:
            return False
        # Sailability: the chord to the circle must be water, not a shoal cut.
        if enter > 0.05 and self.geo.blocked_segment(
            pos, destination(pos, heading, enter)
        ):
            return False
        return True

    def _mark_bearing_clear(
        self,
        pos: tuple[float, float],
        target: tuple[float, float],
        *,
        tws: float | None = None,
        twd: float | None = None,
    ) -> bool:
        rem = distance_nm(pos, target)
        if rem < 1e-4:
            return True
        return self._probe_clear(
            pos,
            bearing(pos, target),
            probe_nm=min(self.p.mark_probe_nm, rem),
            tws=tws,
            twd=twd,
        )

    def _progressive_fan_from_seed(
        self,
        pos: tuple[float, float],
        seed: float,
        tws: float,
        twd: float,
        up_twa: float,
        dn_twa: float,
        *,
        always_extra: list[float] | set[float] | None = None,
        look_nm: float | None = None,
        prefer_brg: float | None = None,
    ) -> list[float]:
        """Widen ±heading_step from mark seed until a clear sailable heading exists.

        Stops at the first clear ring. ``look_nm`` sees an island that a short
        probe would sail into. A clear heading that turns *away* from
        ``prefer_brg`` is skipped until nothing toward the mark is clear, so a
        beat does not peel off into open water on the wrong side of the channel.
        Hard cap is ±180° from seed.
        """
        step = self.p.heading_step_deg
        short = min(
            self.p.fan_probe_nm_max,
            max(self.p.mark_probe_nm * self.p.fan_probe_mark_frac, self.p.fan_probe_nm_min),
        )
        probe_nm = short if look_nm is None else max(short, min(look_nm, 8.0))

        found: list[float] = []
        seen: set[float] = set()

        def consider(h: float, *, allow_away: bool) -> bool:
            key = round(norm360(h), 3)
            if key in seen:
                return False
            twa = twa_from(h, twd)
            # By the lee, same gybe, only when the VMG heading itself is blocked.
            # The fan reaches this a step or two off the seed, before it walks
            # across to the other gybe. Open water never gets here: a clear seed
            # returns on the first test.
            by_the_lee = (
                dn_twa < twa <= min(175.0, dn_twa + 25.0)
                and signed_diff(h, twd) * signed_diff(seed, twd) > 0
            )
            if not (up_twa <= twa <= dn_twa or by_the_lee):
                return False
            if prefer_brg is not None:
                # Even allow_away must not sail more than 90° off the mark —
                # that is the reverse-gybe spike into open water.
                away_limit = 90.0 if allow_away else (
                    abs(signed_diff(seed, prefer_brg)) + 20.0
                )
                if abs(signed_diff(h, prefer_brg)) > away_limit:
                    return False
            if not self._probe_clear(pos, h, probe_nm=probe_nm, tws=tws, twd=twd):
                return False
            seen.add(key)
            found.append(norm360(h))
            return True

        def sweep(allow_away: bool) -> bool:
            if always_extra:
                for h in always_extra:
                    consider(h, allow_away=allow_away)
            if consider(seed, allow_away=allow_away):
                return True
            half = step
            hit = False
            while half <= self.p.fan_half_max_deg + 1e-6:
                hit = consider(norm360(seed + half), allow_away=allow_away) or hit
                hit = consider(norm360(seed - half), allow_away=allow_away) or hit
                if hit:
                    return True
                half += step
            return False

        if not sweep(False):
            seen.clear()
            found.clear()
            if not sweep(True):
                found = [seed]
                if always_extra:
                    found.extend(always_extra)
        return list(dict.fromkeys(found))

    def _seed_heading(
        self,
        node: Node,
        tws: float,
        twd: float,
        guide: tuple[float, float],
        target: tuple[float, float],
        radius_nm: float = 0.2,
    ) -> tuple[float, str, float]:
        """Return (seed_heading, mode, twa_brg).

        Upwind and downwind, hold the long board until the other VMG heading
        enters the mark circle, then take it. That is one tack or one gybe.
        """
        brg = bearing(node.pos, guide)
        up_twa = self.boat.target_upwind_twa(tws)
        dn_twa = self.boat.target_downwind_twa(tws)
        port_up = norm360(twd + up_twa)
        stbd_up = norm360(twd - up_twa)
        port_dn = norm360(twd + dn_twa)
        stbd_dn = norm360(twd - dn_twa)
        twa_brg = twa_from(brg, twd)

        def board(port: float, stbd: float) -> float:
            return self._vmg_board(
                node, port, stbd, brg, guide, target, radius_nm, tws, twd
            )

        if twa_brg < up_twa:
            return board(port_up, stbd_up), "up_vmg", twa_brg
        if twa_brg > dn_twa:
            return board(port_dn, stbd_dn), "dn_vmg", twa_brg
        return brg, "bearing", twa_brg

    def _vmg_board(
        self,
        node: Node,
        port: float,
        stbd: float,
        brg: float,
        guide: tuple[float, float],
        lay_at: tuple[float, float],
        radius_nm: float,
        tws: float,
        twd: float,
    ) -> float:
        """Layline board if one fetches the race mark; otherwise stay on the board already sailed.

        ``guide`` is only the bearing used when neither board lays the mark. A
        headland point a few cables ahead is fetched by both gybes, and treating
        that as the layline flips the boat at every waypoint.
        """

        def clear(heading: float) -> bool:
            rem = distance_nm(node.pos, guide)
            return self._probe_clear(
                node.pos,
                heading,
                probe_nm=min(self.p.mark_probe_nm, max(rem, 0.05)),
                tws=tws,
                twd=twd,
            )

        port_lays = self._heading_fetches(node.pos, port, lay_at, radius_nm) and clear(port)
        stbd_lays = self._heading_fetches(node.pos, stbd, lay_at, radius_nm) and clear(stbd)
        same = None
        if node.heading is not None:
            same = (
                port
                if abs(signed_diff(port, node.heading)) <= abs(signed_diff(stbd, node.heading))
                else stbd
            )
        # Both boards fetch a circle you're already inside. Flipping here is a
        # tack of a few metres.
        if same is not None and distance_nm(node.pos, lay_at) < 0.45 and clear(same):
            return same
        if port_lays or stbd_lays:
            if port_lays and stbd_lays and same is not None:
                return same
            return port if port_lays else stbd
        closer = (
            port
            if abs(signed_diff(port, brg)) <= abs(signed_diff(stbd, brg))
            else stbd
        )
        other = stbd if closer == port else port

        def open_board(heading: float) -> bool:
            """True when the board stays clear for a working boat-length.

            Short ``clear()`` alone accepts a pocket into the island (M2→M3 on
            the IoW south coast: closer board clear at 0.8 nm, land at 1 nm).
            Skip the long probe when the short one already fails.
            """
            if not clear(heading):
                return False
            return self._probe_clear(
                node.pos,
                heading,
                probe_nm=min(1.5, max(1.0, distance_nm(node.pos, guide) * 0.15)),
                tws=tws,
                twd=twd,
            )

        # Only the first board of a leg looks ahead. A persistent shift breaks a
        # tie, or overrides it, toward the side that shift will lift. Mid-leg
        # we stay put until the new layline appears.
        if same is None:
            prefer = self._shift_board(node, port, stbd, brg, twd)
            if prefer is not None and clear(prefer) and open_board(prefer):
                return prefer
            if open_board(closer):
                return closer
            if open_board(other):
                return other
            return closer
        # Held board into a pocket: flip to the open opposite board rather than
        # sailing into the shore under lockout (oscillating / shifted wind).
        if not open_board(same) and open_board(other):
            return other
        # Held board >50° off the aim while the other board is open — stay-put
        # locked a westbound tack away from M3 east of IoW (wind 315).
        if (
            open_board(other)
            and abs(signed_diff(same, brg)) > 50.0
            and abs(signed_diff(other, brg)) + 10.0 < abs(signed_diff(same, brg))
        ):
            return other
        # Post-mark: inbound heading is not a board to protect. Prefer the open
        # board closer to the guide (Stromboli mark snap kept the island gybe).
        if node.maneuver == "mark":
            if open_board(closer):
                return closer
            if open_board(other):
                return other
        # A waypoint bearing closer to the other gybe is not a layline. Stay on
        # this board; if the VMG heading is into land the fan eases off on the
        # same side before it will gybe.
        return same

    def _shift_board(
        self,
        node: Node,
        port: float,
        stbd: float,
        brg: float,
        twd: float,
    ) -> float | None:
        """First-board choice when a shift is present at two future times and same sign.

        The favored side is the opposite of the future board closer to the mark:
        that is the side you must already be on for the new layline to be short.
        """
        samples: list[tuple[float, float]] = []
        for ahead in (900, 1800, 2700, 3600):
            w = self.wind.wind_at(node.lat, node.lon, node.t + timedelta(seconds=ahead))
            if w is None:
                break
            samples.append((w[0], signed_diff(w[1], twd)))
        future_tws = None
        future_delta = 0.0
        for i in range(len(samples) - 1):
            d1, d2 = samples[i][1], samples[i + 1][1]
            if abs(d1) >= 15.0 and abs(d2) >= 15.0 and d1 * d2 > 0:
                future_tws = samples[i + 1][0]
                future_delta = d2
                break
        if future_tws is None:
            return None
        future_twd = norm360(twd + future_delta)
        fup = self.boat.target_upwind_twa(future_tws)
        future_port = norm360(future_twd + fup)
        future_stbd = norm360(future_twd - fup)
        future_closer_port = abs(signed_diff(future_port, brg)) <= abs(
            signed_diff(future_stbd, brg)
        )
        # Start on the other side from the one that will be the short layline.
        return stbd if future_closer_port else port

    def _candidate_headings(
        self,
        node: Node,
        tws: float,
        twd: float,
        target: tuple[float, float],
        radius_nm: float = 0.2,
    ) -> list[float]:
        """Seed mark/VMG → progressive fan → opposite board only once it lays."""
        guide = self._guide(node) if self._guide_pts else target
        brg = bearing(node.pos, guide)
        # Within 2 nm, if the mark line is shoal and the bearing itself is not
        # sailable, creep along the clear heading that ends closest to the mark.
        # That is the way out of a pocket. Farther away the normal layline rules
        # still go around an island.
        if (
            distance_nm(node.pos, target) < 2.0
            and self.geo.blocked_segment(node.pos, target)
            and not self._probe_clear(node.pos, brg, probe_nm=0.4, tws=tws, twd=twd)
        ):
            up_lim = self.boat.target_upwind_twa(tws)
            dn_lim = self.boat.target_downwind_twa(tws)
            lockout_nm = max(0.5, float(self.p.tack_lockout_nm))
            fresh = (
                node.heading is not None
                and self._sailed_since_tack_nm(node) < lockout_nm
            )
            best_h = None
            best_d = 1e9
            best_held = None
            best_held_d = 1e9
            parent = None
            if fresh:
                parent = Node(
                    lat=node.lat,
                    lon=node.lon,
                    t=node.t,
                    heading=node.heading,
                    twd=node.twd,
                )
            for h in range(0, 360, 15):
                heading = float(h)
                twa = twa_from(heading, twd)
                if not (up_lim <= twa <= min(175.0, dn_lim + 25.0)):
                    continue
                if not self._probe_clear(
                    node.pos, heading, probe_nm=0.5, tws=tws, twd=twd
                ):
                    continue
                end = destination(node.pos, heading, 0.5)
                dist = distance_nm(end, target)
                if dist < best_d:
                    best_d = dist
                    best_h = heading
                if parent is not None and _detect_maneuver(
                    parent, heading, twd, twa
                ) == "none":
                    if dist < best_held_d:
                        best_held_d = dist
                        best_held = heading
            # Pocket escape must not thrash under lockout — keep the held board
            # when it still has water, even if an opposite heading is nearer.
            if best_held is not None and (
                best_h is None
                or _detect_maneuver(parent, best_h, twd, twa_from(best_h, twd))
                != "none"
            ):
                return [best_held]
            if best_h is not None:
                return [best_h]
        up_twa = self.boat.target_upwind_twa(tws)
        dn_twa = self.boat.target_downwind_twa(tws)
        port_up = norm360(twd + up_twa)
        stbd_up = norm360(twd - up_twa)
        port_dn = norm360(twd + dn_twa)
        stbd_dn = norm360(twd - dn_twa)
        seed, seed_mode, _twa_brg = self._seed_heading(
            node, tws, twd, guide, target, radius_nm
        )
        # Wrong-side guide can land in bearing mode aimed reverse of the mark
        # (seed ~20°/39° while M1 is west). If the mark is still upwind of a
        # beam reach, force VMG boards — a 70° gate missed mark_twa≈75° and
        # left only the reverse seed in the fan.
        mark_twa = twa_from(bearing(node.pos, target), twd)
        if (
            seed_mode == "bearing"
            and mark_twa < 90.0
            and twa_from(seed, twd) >= 90.0
        ):
            seed, seed_mode, _twa_brg = self._seed_heading(
                node, tws, twd, target, target, radius_nm
            )

        mark_clear = self._mark_bearing_clear(node.pos, guide, tws=tws, twd=twd)
        extras: list[float] = []
        # Sticky current heading only while continuing a leg — never right after
        # a mark snap (inbound heading points at the *previous* mark).
        if (
            node.heading is not None
            and not node.rethink
            and node.maneuver != "mark"
            and not mark_clear
        ):
            extras.append(node.heading)
        # Corridor: after geo rethink, keep prior heading while mark still blocked.
        if node.heading is not None and node.rethink and not mark_clear:
            extras.append(node.heading)

        look = distance_nm(node.pos, guide)
        short_probe = min(
            self.p.fan_probe_nm_max,
            max(self.p.mark_probe_nm * self.p.fan_probe_mark_frac, self.p.fan_probe_nm_min),
        )
        short_ok = self._probe_clear(node.pos, seed, probe_nm=min(short_probe, look), tws=tws, twd=twd)
        long_ok = self._probe_clear(node.pos, seed, probe_nm=min(look, 8.0), tws=tws, twd=twd)
        # Long look only for *bearing* seeds when the rhumb is blocked: creep the
        # corridor. On a VMG upwind/downwind board the long board always hits a
        # shore eventually — that must not open allow_away and walk to a reverse
        # gybe in open water (Solent finish spike to 110° while aiming 266°).
        use_long_look = (
            seed_mode == "bearing" and short_ok and not long_ok
        )
        fanned = self._progressive_fan_from_seed(
            node.pos,
            seed,
            tws,
            twd,
            up_twa,
            dn_twa,
            always_extra=extras or None,
            look_nm=look if use_long_look else None,
            prefer_brg=bearing(node.pos, target),
        )
        # Sorted so candidate order is deterministic (Python set order is not).
        heads: list[float] = []
        for h in (*fanned, seed):
            if h not in heads:
                heads.append(h)

        # Open-water diversity: when pointing at the mark is the seed and clear,
        # offer ±one step so prune can prefer a slightly hotter/cooler angle.
        if seed_mode == "bearing" and mark_clear:
            step = self.p.heading_step_deg
            for h in (norm360(seed + step), norm360(seed - step)):
                if h not in heads:
                    heads.append(h)

        # After mark snap / huge course error, score VMG relative to mark seed
        # not the stale inbound heading.
        ref = seed
        if (
            node.heading is not None
            and node.maneuver != "mark"
            and abs(signed_diff(brg, node.heading)) <= self.p.heading_ref_max_err_deg
        ):
            ref = node.heading
        same_up = (
            port_up
            if abs(signed_diff(port_up, ref)) <= abs(signed_diff(stbd_up, ref))
            else stbd_up
        )
        opp_up = stbd_up if same_up == port_up else port_up
        same_dn = (
            port_dn
            if abs(signed_diff(port_dn, ref)) <= abs(signed_diff(stbd_dn, ref))
            else stbd_dn
        )
        opp_dn = stbd_dn if same_dn == port_dn else port_dn
        # Already eased off to keep a gybe off the shore. Stay there until the
        # VMG heading is clear for a mile — turning back for one boat-length and
        # easing off again is the zigzag in steady wind.
        if (
            seed_mode == "dn_vmg"
            and node.heading is not None
            and node.maneuver != "mark"
            and not node.rethink
        ):
            held_twa = twa_from(node.heading, twd)
            eased = (
                dn_twa < held_twa <= min(175.0, dn_twa + 25.0)
                and signed_diff(node.heading, twd) * signed_diff(seed, twd) > 0
                and self._probe_clear(
                    node.pos, node.heading, probe_nm=0.4, tws=tws, twd=twd
                )
            )
            vmg_open = self._probe_clear(
                node.pos,
                seed,
                probe_nm=min(1.0, distance_nm(node.pos, target)),
                tws=tws,
                twd=twd,
            )
            if eased and not vmg_open:
                held = [node.heading]
                if self._heading_fetches(node.pos, opp_dn, target, radius_nm):
                    held.append(opp_dn)
                return held
        if seed_mode == "up_vmg":
            if same_up not in heads:
                heads.append(same_up)
            # Offer the other board when it lays, OR when the near board is a
            # shore pocket (clear short, blocked at 1 nm) — otherwise M2→M3
            # only ever fans the island-bound tack.
            same_open = self._probe_clear(
                node.pos,
                same_up,
                probe_nm=min(1.5, max(1.0, distance_nm(node.pos, target) * 0.1)),
                tws=tws,
                twd=twd,
            )
            opp_open = self._probe_clear(
                node.pos, opp_up, probe_nm=0.4, tws=tws, twd=twd
            )
            wrong_tack = (
                node.heading is not None
                and abs(
                    signed_diff(
                        node.heading, self._ban_aim_bearing(node, target)
                    )
                )
                > 50.0
            )
            if self._heading_fetches(node.pos, opp_up, target, radius_nm) or (
                not same_open and opp_open
            ) or (wrong_tack and opp_open):
                if opp_up not in heads:
                    heads.append(opp_up)
        elif seed_mode == "dn_vmg":
            if same_dn not in heads:
                heads.append(same_dn)
            same_dn_open = self._probe_clear(
                node.pos,
                same_dn,
                probe_nm=min(1.5, max(1.0, distance_nm(node.pos, target) * 0.1)),
                tws=tws,
                twd=twd,
            )
            opp_dn_open = self._probe_clear(
                node.pos, opp_dn, probe_nm=0.4, tws=tws, twd=twd
            )
            # Same pocket rule as up_vmg — Stromboli→Favignana seeded the
            # island-bound gybe because only a geometric fetch opened the other.
            if self._heading_fetches(node.pos, opp_dn, target, radius_nm) or (
                not same_dn_open and opp_dn_open
            ):
                if opp_dn not in heads:
                    heads.append(opp_dn)

        # A tack/gybe inside the lockout distance is the Solent thrash. Allow the
        # opposite board only once the held board is into shore (real channel
        # edge) or the other board already lays the mark.
        lockout_nm = max(0.5, float(self.p.tack_lockout_nm))
        fresh_tack = self._sailed_since_tack_nm(node) < lockout_nm

        out: list[float] = []
        forced = {seed, same_up, same_dn, opp_up, opp_dn, *extras}
        for h in heads:
            twa = twa_from(h, twd)
            by_the_lee = (
                seed_mode == "dn_vmg"
                and dn_twa < twa <= min(175.0, dn_twa + 25.0)
                and signed_diff(h, twd) * signed_diff(seed, twd) > 0
            )
            if h in forced or up_twa <= twa <= dn_twa or by_the_lee:
                out.append(h)
        # Already >50° off the race aim: lockout must not trap us on a reverse board.
        wrong_way_held = (
            node.heading is not None
            and abs(signed_diff(node.heading, self._ban_aim_bearing(node, target)))
            > 50.0
        )
        # Wind flip / pinch: held board is no longer sailable — release lockout
        # and do not re-sticky that heading in later filters (oscillating westerly
        # left only an unsailable 16° TWA board).
        held_sailable = node.heading is None or self.boat.bsp(
            tws, twa_from(node.heading, twd)
        ) > 0.05
        if (
            fresh_tack
            and node.heading is not None
            and not wrong_way_held
            and held_sailable
        ):
            parent = Node(
                lat=node.lat,
                lon=node.lon,
                t=node.t,
                heading=node.heading,
                twd=node.twd,
            )
            held = [
                h
                for h in out
                if _detect_maneuver(parent, h, twd, twa_from(h, twd)) == "none"
            ]
            # Always treat the live heading as held — fan/seed may have dropped it.
            if node.heading not in held:
                held.append(node.heading)
            short_probe = min(
                self.p.fan_probe_nm_min, max(distance_nm(node.pos, guide), 0.05)
            )
            shore_probe = max(short_probe, float(self.p.tack_shore_probe_nm))
            clear_held = [
                h
                for h in held
                if self._probe_clear(node.pos, h, probe_nm=short_probe, tws=tws, twd=twd)
            ]
            # Shore means NO held heading is clear for a real boat-length.
            # A failed long probe with a clear short probe is mid-channel noise —
            # that used to open the opposite board and thrash every 0.6 nm.
            held_into_shore = not any(
                self._probe_clear(node.pos, h, probe_nm=shore_probe, tws=tws, twd=twd)
                for h in held
            )
            # Held board is about to leave sea-room while opposite still closes —
            # open the other board under lockout.
            if (
                not held_into_shore
                and node.heading is not None
                and self._held_board_deadends_sooner(
                    node.pos, node.heading, target, tws, twd, radius_nm
                )
            ):
                held_into_shore = True
            lays = []
            if distance_nm(node.pos, target) >= 0.45:
                lays = [
                    h
                    for h in out
                    if h not in held
                    and self._heading_fetches(node.pos, h, target, radius_nm)
                ]
            mark_twa = twa_from(self._ban_aim_bearing(node, target), twd)
            # Opposite boards must come from the full fan/VMG set, not from
            # `out` after held-only filtering (that made shore-escape a no-op).
            opp_pool = [
                h
                for h in (*heads, same_up, opp_up, same_dn, opp_dn, seed)
                if h not in held
            ]

            def _opp_upwind() -> list[float]:
                opp: list[float] = []
                for h in opp_pool:
                    if not self._probe_clear(
                        node.pos, h, probe_nm=short_probe, tws=tws, twd=twd
                    ):
                        continue
                    htwa = twa_from(h, twd)
                    # Ban downwind escapes on an upwind leg. Do NOT also require
                    # |heading-aim|≤90° — the other close-hauled board is often
                    # ~90–110° off the mark and is the only Needles escape.
                    if mark_twa <= 70.0 and htwa >= 90.0:
                        continue
                    opp.append(h)
                return list(dict.fromkeys(opp))

            def _pocket_upwind() -> list[float]:
                """Scan eased upwind headings when both VMG boards are blocked."""
                aim = self._ban_aim_bearing(node, target)
                dist0 = distance_nm(node.pos, target)
                probe = max(short_probe, 0.3)

                def scan(max_away: float, *, upwind_only: bool) -> list[float]:
                    found: list[float] = []
                    for deg in range(0, 360, 15):
                        h = float(deg)
                        htwa = twa_from(h, twd)
                        if htwa < up_twa - 1.0:
                            continue
                        if upwind_only and htwa >= 90.0:
                            continue
                        if not upwind_only and htwa > min(175.0, dn_twa + 25.0):
                            continue
                        if not self._probe_clear(
                            node.pos, h, probe_nm=short_probe, tws=tws, twd=twd
                        ):
                            continue
                        end = destination(node.pos, h, probe)
                        if distance_nm(end, target) > dist0 + max_away:
                            continue
                        found.append(h)
                    found.sort(
                        key=lambda h: (
                            distance_nm(destination(node.pos, h, probe), target),
                            abs(signed_diff(h, aim)),
                        )
                    )
                    return found[:4]

                # Prefer water that closes the mark; if the pocket has none,
                # allow a short lateral creep (Needles) rather than dying.
                # Last resort: bear away into clear water when every upwind
                # heading is shoal (Bembridge shelf on M2→M3 @315).
                return (
                    scan(0.15, upwind_only=True)
                    or scan(0.45, upwind_only=True)
                    or scan(0.6, upwind_only=False)
                )

            if clear_held and not held_into_shore:
                # Mid-channel lockout: stay on the held board. Layline only.
                out = [*clear_held, *lays]
            elif clear_held and held_into_shore:
                # Short probe OK but shore/shelf look-ahead says leave soon —
                # keep a creep on held AND allow a clear opposite upwind board.
                out = [*clear_held, *lays, *_opp_upwind()]
            elif held_into_shore or not held:
                # Truly stuck: opposite VMG board, else eased pocket scan.
                opp = _opp_upwind() or _pocket_upwind()
                out = opp if opp else (lays if lays else clear_held or held)
            else:
                # Held exists but short-probe failed (whisker): prefer lays, else held.
                out = lays if lays else held
                if not out or (
                    node.heading is not None
                    and not self._probe_clear(
                        node.pos, node.heading, probe_nm=short_probe, tws=tws, twd=twd
                    )
                ):
                    pocket = _pocket_upwind()
                    if pocket:
                        out = pocket
        elif wrong_way_held:
            # Escape the reverse board: prefer seed / anything toward the aim.
            aim_brg = self._ban_aim_bearing(node, target)
            rescue = [
                h
                for h in out
                if self._heading_progresses(node, h, target)
                and abs(signed_diff(h, aim_brg)) <= 50.0
            ]
            if not rescue and self._heading_progresses(node, seed, target):
                rescue = [seed]
            if rescue:
                out = rescue
        out = list(dict.fromkeys(out))
        # Drop headings that increase chain rem. Always — even when the rhumb to
        # the guide clips shoal — otherwise Solent wrong-way gybes stay in the fan.
        if out and distance_nm(node.pos, target) > 1.5:
            def _open_nm(h: float, nm: float = 1.0) -> bool:
                return self._probe_clear(
                    node.pos, h, probe_nm=nm, tws=tws, twd=twd
                )

            ranked = sorted(
                out,
                key=lambda h: self._rem_after_heading(node, h, target),
            )
            progressed = [
                h for h in ranked if self._heading_progresses(node, h, target)
            ]
            # A short step into a shore pocket can look like rem progress while
            # the open opposite board costs a fraction of a mile of rem. Prefer
            # progressed headings that stay clear for 1 nm; else keep an open seed.
            open_prog = [h for h in progressed if _open_nm(h)]
            if open_prog:
                out = open_prog
            elif _open_nm(seed):
                out = [seed]
            elif progressed:
                out = progressed
            else:
                # Nothing in the fan progresses — do NOT keep a reverse heading.
                # Fall back to seed / held when those reduce rem and stay open.
                fallback: list[float] = []
                for h in (seed, node.heading):
                    if h is None:
                        continue
                    if self._heading_progresses(node, float(h), target) and _open_nm(
                        float(h)
                    ):
                        fallback.append(float(h))
                if not fallback:
                    for h in (seed, node.heading):
                        if h is None:
                            continue
                        if self._heading_progresses(node, float(h), target):
                            fallback.append(float(h))
                out = fallback if fallback else ranked[:1]
        # Hard ban: never sail >90° off the race aim *downwind*. Upwind boards
        # (twa<90) are allowed even when ~100° off the mark — that is a normal
        # beat, and banning them stranded the Needles approach. Skip inside
        # 1.5 nm so a final lay/creep is not stripped.
        if out and distance_nm(node.pos, target) > 1.5:
            brg_aim = self._ban_aim_bearing(node, target)
            kept = [
                h
                for h in out
                if abs(signed_diff(h, brg_aim)) <= 90.0 or twa_from(h, twd) < 90.0
            ]
            if kept:
                out = kept
            elif (
                held_sailable
                and node.heading is not None
                and (
                    abs(signed_diff(node.heading, brg_aim)) <= 90.0
                    or twa_from(node.heading, twd) < 90.0
                )
            ):
                out = [node.heading]
            else:
                out = [seed]
        # Upwind-to-mark: never offer a downwind/reach gybe unless it lays.
        # That was the V-spike south-then-north on the Solent westbound beat.
        if out and distance_nm(node.pos, target) > 1.5:
            aim_twa = twa_from(self._ban_aim_bearing(node, target), twd)
            if aim_twa < 90.0:
                up_only = [
                    h
                    for h in out
                    if twa_from(h, twd) < 90.0
                    or self._heading_fetches(node.pos, h, target, radius_nm)
                ]
                if up_only:
                    out = up_only
                elif twa_from(seed, twd) < 90.0:
                    out = [seed]
                else:
                    # Seed itself is a reverse gybe — force the upwind VMG board.
                    out = [same_up]
                # Always keep a clear held upwind board — dropping it for a
                # reverse seed emptied the Needles frontier. Skip when the held
                # board is pinched/unsailable after a wind flip, and skip right
                # after a mark snap (inbound heading points at the last mark /
                # into the island — that re-stuck M2→M3 on a full Solent loop).
                if (
                    held_sailable
                    and node.maneuver != "mark"
                    and node.heading is not None
                    and twa_from(node.heading, twd) < 90.0
                    and self._probe_clear(
                        node.pos,
                        node.heading,
                        probe_nm=min(self.p.fan_probe_nm_min, 0.25),
                        tws=tws,
                        twd=twd,
                    )
                    and node.heading not in out
                ):
                    out = [node.heading, *out]
        # Drop pinched / zero-bsp headings (wind flip mid-leg).
        out = [h for h in out if self.boat.bsp(tws, twa_from(h, twd)) > 0.05]
        # Empty fan (filters removed everything) — sail the seed.
        if not out:
            out = [seed] if self.boat.bsp(tws, twa_from(seed, twd)) > 0.05 else [same_up]
        # Fan / seed into shore (or stranded on a blocked cell): bear-away scan.
        # Gated on seed/blocked — not "any out head" — so open-water Fastnet
        # does not pay a 24-heading probe every expand.
        short_escape = min(
            self.p.fan_probe_nm_min, max(distance_nm(node.pos, guide), 0.05)
        )
        seed_blocked = self.geo.blocked_point(node.pos) or not self._probe_clear(
            node.pos, seed, probe_nm=short_escape, tws=tws, twd=twd
        )
        if out and seed_blocked and not any(
            self._probe_clear(node.pos, h, probe_nm=short_escape, tws=tws, twd=twd)
            for h in out
        ):
            aim = self._ban_aim_bearing(node, target)
            dist0 = distance_nm(node.pos, target)
            escape: list[float] = []
            for deg in range(0, 360, 15):
                h = float(deg)
                htwa = twa_from(h, twd)
                if htwa < up_twa - 1.0 or htwa > min(175.0, dn_twa + 25.0):
                    continue
                if not self._probe_clear(
                    node.pos, h, probe_nm=short_escape, tws=tws, twd=twd
                ):
                    continue
                end = destination(node.pos, h, max(short_escape, 0.3))
                if distance_nm(end, target) > dist0 + 0.6:
                    continue
                escape.append(h)
            escape.sort(
                key=lambda h: (
                    distance_nm(
                        destination(node.pos, h, max(short_escape, 0.3)), target
                    ),
                    abs(signed_diff(h, aim)),
                )
            )
            if escape:
                out = escape[:4]
        # A 60° gybe beside a headland is the spike: the long probe only liked a
        # heading aimed at open water. Turn at most 25° when a nearer heading is
        # clear for a quarter mile, and keep a layline tack or gybe that fetches
        # the race mark.
        wrong_way = (
            node.heading is not None
            and abs(signed_diff(node.heading, bearing(node.pos, guide))) > 50.0
        )
        if (
            node.heading is not None
            and node.maneuver != "mark"
            and not wrong_way
            and held_sailable
        ):
            near = [
                h
                for h in out
                if abs(signed_diff(h, node.heading)) <= 25.0
                and self._probe_clear(node.pos, h, probe_nm=0.25, tws=tws, twd=twd)
            ]
            if near:
                lays: list[float] = []
                if distance_nm(node.pos, target) >= 0.45:
                    lays = [
                        h
                        for h in out
                        if h not in near
                        and self._heading_fetches(node.pos, h, target, radius_nm)
                    ]
                out = [*near, *lays]
        return out

    def _rem_after_heading(
        self, node: Node, heading: float, tgt: tuple[float, float]
    ) -> float:
        """Chain rem after a short probe on ``heading`` (inf if the probe is blocked)."""
        probe = max(0.2, float(self.p.progress_probe_nm))
        end = destination(node.pos, heading, probe)
        if self.geo.blocked_point(end) or self._segment_blocked_ahead(node.pos, end):
            return 1e9
        trial = Node(
            lat=end[0],
            lon=end[1],
            t=node.t,
            heading=heading,
            guide_idx=node.guide_idx,
        )
        return self._rem(trial, tgt)

    def _heading_progresses(
        self, node: Node, heading: float, tgt: tuple[float, float]
    ) -> bool:
        """True when a short step on ``heading`` does not increase chain rem."""
        after = self._rem_after_heading(node, heading, tgt)
        # Blocked probe is not progress — keeping it let reverse gybes survive.
        if after >= 1e9:
            return False
        return after <= self._rem(node, tgt) + float(self.p.progress_rem_slack_nm)

    def _sailed_since_tack_nm(self, node: Node) -> float:
        """Distance sailed since the most recent tack or gybe on this leg.

        Mark snaps start a new leg. Walking into the previous leg's tack history
        would keep the post-snap fan locked to the inbound board — and that board
        points at the mark just rounded, so every candidate geo-hits (Stromboli
        → Favignana died in two steps with a 357-node parent chain).

        Once we have walked past the lockout distance without finding a maneuver,
        return a large value so the lockout releases (returning the walked
        distance itself was wrong once lockout grew above 1 nm).
        """
        lockout = max(0.5, float(self.p.tack_lockout_nm))
        dist = 0.0
        cur: Node | None = node
        while cur is not None and cur.parent is not None:
            if cur.maneuver == "mark":
                return 999.0
            dist += distance_nm(cur.parent.pos, cur.pos)
            if cur.maneuver in ("tack", "gybe"):
                return dist
            if dist >= lockout:
                return 999.0
            cur = cur.parent
        return dist if cur is not None and cur.maneuver in ("tack", "gybe") else 999.0

    def _score(self, node: Node, target: tuple[float, float]) -> float:
        return _prune_score(
            node,
            self._rem(node, target),
            self.p.maneuver_prune_nm,
            self.p.max_consecutive_maneuvers,
            self.p.maneuver_overflow_nm,
            self.p,
        )

    def _prune(
        self,
        nodes: list[Node],
        target: tuple[float, float],
        origin: tuple[float, float],
    ) -> list[Node]:
        if not nodes:
            return nodes
        best_dmg = min(nodes, key=lambda n: self._rem(n, target))
        by_sector: dict[int, Node] = {}
        for n in nodes:
            sec = int(bearing(origin, n.pos) // self.p.sector_width_deg)
            cur = by_sector.get(sec)
            if cur is None or self._score(n, target) < self._score(cur, target):
                by_sector[sec] = n
        kept = list(by_sector.values())
        by_cell: dict[tuple[int, int], Node] = {}
        for n in kept:
            cell = (
                int(n.lat / self.p.cluster_cell_deg),
                int(n.lon / self.p.cluster_cell_deg),
            )
            cur = by_cell.get(cell)
            if cur is None or self._score(n, target) < self._score(cur, target):
                by_cell[cell] = n
        kept = list(by_cell.values())
        if best_dmg not in kept:
            kept.append(best_dmg)
        if len(kept) > self.p.max_frontier:
            kept.sort(key=lambda n: self._score(n, target))
            trimmed = kept[: self.p.max_frontier]
            if best_dmg not in trimmed:
                trimmed[-1] = best_dmg
            kept = trimmed
        return kept

    def _wind_changed(
        self, tws0: float, twd0: float, tws1: float, twd1: float
    ) -> bool:
        if abs(tws1 - tws0) >= self.p.wind_delta_tws_kn:
            return True
        # Near-calm TWD is ill-defined; direction deltas alone must not abort commits.
        if min(tws0, tws1) < self.p.light_air_twd_tws_kn:
            return False
        return abs(signed_diff(twd1, twd0)) >= self.p.wind_delta_twd_deg

    def _segment_blocked_ahead(
        self, start: tuple[float, float], end: tuple[float, float]
    ) -> bool:
        """Blocked-segment test that can leave a already-illegal start cell.

        ``GeoContext.blocked_segment`` samples the start point, so a boat that
        committed onto a depth pocket can never take another step. When the
        start is already blocked, only land-crossings and *ahead* samples count.
        """
        if self.geo.land.segment_crosses_land(start, end):
            return True
        if self.geo.blocked_point(end):
            return True
        skip_start = self.geo.blocked_point(start)
        if not skip_start and self.geo.blocked_segment(start, end):
            return True
        if not skip_start:
            return False
        dist = distance_nm(start, end)
        steps = max(2, int(dist / max(0.05, float(self.geo.segment_sample_nm))))
        for i in range(1, steps + 1):
            f = i / steps
            lat = start[0] + f * (end[0] - start[0])
            lon = start[1] + f * (end[1] - start[1])
            if self.geo.blocked_point((lat, lon)):
                return True
        return False

    def _last_legal_point(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> tuple[float, float] | None:
        if not self._segment_blocked_ahead(start, end) and not self.geo.blocked_point(
            end
        ):
            return end
        # Leave a stranded start by walking toward the first clear sample.
        if self.geo.blocked_point(start):
            dist = distance_nm(start, end)
            steps = max(4, int(dist / max(0.05, float(self.geo.segment_sample_nm))))
            for i in range(1, steps + 1):
                f = i / steps
                mid = (
                    start[0] + f * (end[0] - start[0]),
                    start[1] + f * (end[1] - start[1]),
                )
                if not self.geo.blocked_point(mid) and not self.geo.land.segment_crosses_land(
                    start, mid
                ):
                    return mid
            return None
        lo_lat, lo_lon = start
        hi_lat, hi_lon = end
        last_ok: tuple[float, float] | None = start
        for _ in range(10):
            mid = ((lo_lat + hi_lat) * 0.5, (lo_lon + hi_lon) * 0.5)
            if self._segment_blocked_ahead(start, mid) or self.geo.blocked_point(mid):
                hi_lat, hi_lon = mid
            else:
                last_ok = mid
                lo_lat, lo_lon = mid
        if last_ok is None or distance_nm(start, last_ok) < 1e-4:
            return None
        return last_ok

    def _should_rethink_heading(
        self,
        *,
        heading: float,
        pos: tuple[float, float],
        tgt: tuple[float, float],
        tws: float,
        twd: float,
    ) -> bool:
        """Stop escape/beat commit when the target is free again or past layline.

        ``tgt`` is the active guidance point, so rounding a headland ends the
        commit and re-seeds against the next one.
        """
        brg = bearing(pos, tgt)
        if abs(signed_diff(heading, brg)) < self.p.rethink_heading_vs_bearing_deg:
            return False
        up_twa = self.boat.target_upwind_twa(tws)
        dn_twa = self.boat.target_downwind_twa(tws)
        twa_brg = twa_from(brg, twd)
        # A reach aims at the guide once that line is open. A beat or a run
        # deliberately sails off the bearing, so "not pointed at the waypoint"
        # is not a reason to turn. Past the layline still is.
        if up_twa <= twa_brg <= dn_twa:
            if not self._mark_bearing_clear(pos, tgt, tws=tws, twd=twd):
                return False
            return abs(signed_diff(heading, brg)) > self.p.rethink_heading_vs_bearing_deg
        return abs(signed_diff(brg, heading)) > self.p.past_layline_deg

    def _other_board_lays(
        self,
        heading: float,
        pos: tuple[float, float],
        mark: tuple[float, float],
        tws: float,
        twd: float,
        radius_nm: float,
    ) -> bool:
        """True when the board we are not on now fetches the race mark.

        Checked against the mark, not a headland point, so a commit ends on the
        layline instead of sailing past it.
        """
        twa_brg = twa_from(bearing(pos, mark), twd)
        up_twa = self.boat.target_upwind_twa(tws)
        dn_twa = self.boat.target_downwind_twa(tws)
        if twa_brg < up_twa:
            a, b = norm360(twd + up_twa), norm360(twd - up_twa)
        elif twa_brg > dn_twa:
            a, b = norm360(twd + dn_twa), norm360(twd - dn_twa)
        else:
            return False
        other = a if abs(signed_diff(a, heading)) >= abs(signed_diff(b, heading)) else b
        if abs(signed_diff(other, heading)) < 20.0:
            return False
        if not self._heading_fetches(pos, other, mark, radius_nm):
            return False
        rem = distance_nm(pos, mark)
        return self._probe_clear(
            pos,
            other,
            probe_nm=min(self.p.mark_probe_nm, max(rem, 0.05)),
            tws=tws,
            twd=twd,
        )

    def _dt_on_planned_wind(
        self,
        lat: float,
        lon: float,
        t: datetime,
        heading: float,
        tws0: float,
        twd0: float,
        dt: float,
    ) -> float:
        """Shorten a substep that would be sailed on a different wind.

        A square-wave shift faster than ``time_step_s`` otherwise lands the
        midpoint sample in the no-go and the commit returns no movement.
        """

        def stable(dt_try: float) -> bool:
            if dt_try < 5.0:
                return False
            w = self.wind.wind_at(lat, lon, t + timedelta(seconds=dt_try * 0.5))
            if w is None:
                return False
            tws, twd = w
            if self._wind_changed(tws0, twd0, tws, twd):
                return False
            return self.boat.bsp(tws, twa_from(heading, twd)) > 0.05

        if stable(dt):
            return dt
        lo, hi = 5.0, dt
        best = 0.0
        for _ in range(8):
            mid = (lo + hi) * 0.5
            if stable(mid):
                best = mid
                lo = mid
            else:
                hi = mid
        if best >= 5.0:
            return best
        # No midpoint stays on this wind. Sail a short slice on the wind now.
        w_now = self.wind.wind_at(lat, lon, t)
        if w_now is None:
            return 0.0
        if self.boat.bsp(w_now[0], twa_from(heading, w_now[1])) <= 0.05:
            return 0.0
        return min(dt, 15.0)

    def _commit_heading(
        self,
        node: Node,
        heading: float,
        tgt: tuple[float, float],
        *,
        max_commit_s: float,
        tws0: float,
        twd0: float,
        arrive_nm: float = 0.0,
    ) -> tuple[Node | None, bool]:
        """Hold heading until rethink (mark clear / layline / wind / geo) or cap.

        Guidance is refreshed each substep: a waypoint chain that clears mid-
        commit (mark comes into sight past a headland) must stop the underlay
        onto the old corner and re-seed onto the mark, not sail low then snap.
        """
        guide = self._guide(node)
        guide_idx = node.guide_idx
        twa0 = twa_from(heading, twd0)
        bsp0 = self.boat.bsp(tws0, twa0)
        if bsp0 <= 0:
            return None, False

        maneuver = _detect_maneuver(node, heading, twd0, twa0)
        # Lockout at commit (candidates already filter): only refuse a maneuver
        # when the held board still has shore-probe water and the new heading
        # neither lays nor recovers from a reverse board. Harder bans live in
        # _candidate_headings — repeating them here stranded the Needles approach.
        if (
            maneuver in ("tack", "gybe")
            and node.heading is not None
            and node.maneuver != "mark"
        ):
            lockout_nm = max(0.5, float(self.p.tack_lockout_nm))
            if self._sailed_since_tack_nm(node) < lockout_nm:
                shore = max(0.5, float(self.p.tack_shore_probe_nm))
                held_bsp = self.boat.bsp(
                    tws0, twa_from(node.heading, twd0)
                )
                # Pinch after a wind flip is as stuck as shore — probe_clear only
                # checks geo, so an unsailable held board looked "fine" and the
                # commit lockout refused the only sailable tack (osc finish).
                held_into_shore = held_bsp <= 0.05 or not self._probe_clear(
                    node.pos, node.heading, probe_nm=shore, tws=tws0, twd=twd0
                )
                if not held_into_shore and self._held_board_deadends_sooner(
                    node.pos, node.heading, tgt, tws0, twd0, max(arrive_nm, 0.2)
                ):
                    held_into_shore = True
                lays = self._heading_fetches(
                    node.pos, heading, tgt, max(arrive_nm, 0.2)
                )
                brg_aim = self._ban_aim_bearing(node, tgt)
                reverse = abs(signed_diff(node.heading, brg_aim)) > 50.0
                recovering = abs(signed_diff(heading, brg_aim)) <= 50.0
                if not lays and not held_into_shore and not (reverse and recovering):
                    return None, False
        pen = 0.0
        if maneuver == "tack":
            pen = self.boat.config.tack_penalty_s
        elif maneuver == "gybe":
            pen = self.boat.config.gybe_penalty_s
        elif node.heading is not None and node.maneuver != "mark" and not node.rethink:
            pen = _course_change_penalty_s(
                signed_diff(heading, node.heading),
                tws0,
                self.boat.config.tack_penalty_s,
                self.p,
            )

        budget = max_commit_s
        epoch_s = _seconds_until_next_wind_epoch(self.wind, node.t)
        if epoch_s is not None:
            budget = min(
                budget,
                max(self.p.time_step_s * self.p.wind_epoch_commit_frac, epoch_s - 1.0),
            )
        # Never step past the last wind sample (else mid-step wind_at → None).
        wind_left = _seconds_of_wind_remaining(self.wind, node.t)
        if wind_left is not None:
            budget = min(budget, max(0.0, wind_left - 1.0))
        # Clip to the *current objective*, not the whole corridor: budgeting the
        # full way round a headland lets one commit sail straight past the bend.
        # Without a chain the guide is the mark, so this is unchanged.
        # Budget against the farther of guide and a fraction of mark rem. A
        # wrong-side guide a boat-length away used to zero the budget and kill
        # every Needles escape commit (bsp≈7kn × 0.01nm → budget < 1s).
        rem0 = max(
            distance_nm(node.pos, guide),
            min(distance_nm(node.pos, tgt), 3.0) * 0.25,
            0.4,
        )
        if bsp0 > 0:
            budget = min(
                budget, (rem0 / bsp0) * 3600.0 + self.p.commit_arrival_buffer_s
            )
        # After a tack/gybe, re-evaluate soon so we don't overstand the new layline.
        if maneuver in ("tack", "gybe"):
            budget = min(budget, float(self.p.time_step_s))
        # Constraint mode: creep one substep while mark bearing is blocked.
        if not self._mark_bearing_clear(node.pos, guide, tws=tws0, twd=twd0):
            budget = min(budget, float(self.p.time_step_s))
        # Cap the commit to remaining sea-room so the first substep cannot
        # jump past the 6 m shelf after a rethink (Gosport oversail).
        if twa0 < 90.0 and bsp0 > 0:
            room = self._board_room_nm(
                node.pos,
                heading,
                tws=tws0,
                twd=twd0,
                max_nm=float(self.p.board_room_lookout_nm),
            )
            if room < float(self.p.board_room_lookout_nm):
                sail_nm = max(0.15, room * 0.85)
                budget = min(budget, (sail_nm / bsp0) * 3600.0 + 5.0)
        if budget < 1.0:
            return None, False

        lat, lon = node.lat, node.lon
        t = node.t
        elapsed = 0.0
        sail_elapsed = 0.0
        last_tws, last_twd = tws0, twd0
        last_bsp, last_twa = bsp0, twa0
        sub = float(self.p.time_step_s)
        geo_hit = False
        need_rethink = False
        arrive_r = arrive_nm if arrive_nm > 0 else self.p.mark_arrival_break_nm

        while elapsed + 1e-6 < budget:
            dt = min(sub, budget - elapsed)
            sail_dt = dt
            if elapsed < 1e-9 and pen > 0:
                sail_dt = max(0.0, dt - pen)

            if elapsed > 1e-6 and self._other_board_lays(
                heading, (lat, lon), tgt, last_tws, last_twd, arrive_r
            ):
                need_rethink = True
                break
            # Next tack would be trapped further along this board, but is open
            # here → rethink now (allows shallow boards; blocks oversail pockets).
            if elapsed > 1e-6 and self._held_board_deadends_sooner(
                (lat, lon), heading, tgt, last_tws, last_twd, arrive_r
            ):
                need_rethink = True
                break

            dt = self._dt_on_planned_wind(
                lat, lon, t, heading, tws0, twd0, dt
            )
            if dt < 5.0:
                need_rethink = True
                break
            sail_dt = dt
            if elapsed < 1e-9 and pen > 0:
                sail_dt = max(0.0, dt - pen)
            w = self.wind.wind_at(lat, lon, t + timedelta(seconds=min(dt * 0.5, 1.0)))
            if w is None:
                # Fall back to the wind we planned this step on.
                w = (tws0, twd0)
            tws, twd = w
            # Only rethink on wind change after we have already sailed — otherwise
            # light-air TWD noise aborts the first substep with elapsed=0 and the
            # frontier dies (false frontier_empty through a calm pocket).
            if elapsed > 1e-6 and self._wind_changed(tws0, twd0, tws, twd):
                need_rethink = True
                break

            twa = twa_from(heading, twd)
            bsp = self.boat.bsp(tws, twa)
            if bsp <= 0:
                # The sample drifted into the no-go. Sail this slice on the
                # wind at the start of the slice, which was sailable.
                tws, twd = tws0, twd0
                twa = twa_from(heading, twd)
                bsp = self.boat.bsp(tws, twa)
                if bsp <= 0:
                    break

            ctw = heading + self.boat.leeway_offset(tws, bsp, twd, heading)
            vx, vy = vector(norm360(ctw), bsp)
            nlat, nlon = _advance(lat, lon, vx, vy, sail_dt)
            if self._segment_blocked_ahead((lat, lon), (nlat, nlon)) or self.geo.blocked_point(
                (nlat, nlon)
            ):
                legal = self._last_legal_point((lat, lon), (nlat, nlon))
                if legal is None:
                    geo_hit = True
                    need_rethink = True
                    break
                frac = distance_nm((lat, lon), legal) / max(
                    distance_nm((lat, lon), (nlat, nlon)), 1e-9
                )
                lat, lon = legal
                elapsed += dt * frac
                sail_elapsed += sail_dt * frac
                t = node.t + timedelta(seconds=elapsed)
                last_tws, last_twd = tws, twd
                last_bsp, last_twa = bsp, twa
                geo_hit = True
                need_rethink = True
                break

            arrive = arrive_nm if arrive_nm > 0 else self.p.mark_arrival_break_nm
            if distance_nm((nlat, nlon), tgt) <= arrive and distance_nm((lat, lon), tgt) > arrive:
                lo, hi = 0.0, 1.0
                best_f = 1.0
                for _ in range(10):
                    mid = (lo + hi) * 0.5
                    mlat = lat + (nlat - lat) * mid
                    mlon = lon + (nlon - lon) * mid
                    if distance_nm((mlat, mlon), tgt) <= arrive:
                        best_f = mid
                        hi = mid
                    else:
                        lo = mid
                nlat = lat + (nlat - lat) * best_f
                nlon = lon + (nlon - lon) * best_f
                dt *= best_f
                sail_dt *= best_f

            lat, lon = nlat, nlon
            elapsed += dt
            sail_elapsed += sail_dt
            t = node.t + timedelta(seconds=elapsed)
            last_tws, last_twd = tws, twd
            last_bsp, last_twa = bsp, twa

            if distance_nm((lat, lon), tgt) <= arrive:
                break

            # Re-resolve chain aim after moving. A stale corner bearing is what
            # produced the underlay→snap kinks once the mark came into sight.
            live = Node(
                lat=lat,
                lon=lon,
                t=t,
                heading=heading,
                guide_idx=guide_idx,
            )
            live_guide = self._guide(live)
            if live.guide_idx != guide_idx:
                guide_idx = live.guide_idx
                guide = live_guide
                need_rethink = True
                break
            guide_idx = live.guide_idx
            guide = live_guide
            if self._should_rethink_heading(
                heading=heading,
                pos=(lat, lon),
                tgt=live_guide,
                tws=last_tws,
                twd=last_twd,
            ):
                need_rethink = True
                break

        arrive_now = arrive_nm if arrive_nm > 0 else self.p.mark_arrival_break_nm
        on_mark = distance_nm((lat, lon), tgt) <= arrive_now + 1e-3
        if elapsed < 1.0 and sail_elapsed < 1.0 and not on_mark:
            return None, geo_hit

        # The sailed substeps can bend around a point while the straight chord
        # between the commit ends cuts land. Keep the furthest point whose
        # chord from the start of this commit is still clear.
        blocked_chord = self._segment_blocked_ahead(node.pos, (lat, lon)) or self.geo.blocked_point(
            (lat, lon)
        )
        if blocked_chord:
            legal = self._last_legal_point(node.pos, (lat, lon))
            if legal is None:
                return None, True
            frac = distance_nm(node.pos, legal) / max(distance_nm(node.pos, (lat, lon)), 1e-9)
            lat, lon = legal
            elapsed *= frac
            sail_elapsed *= frac
            geo_hit = True
            need_rethink = True
            if elapsed < 1.0 and sail_elapsed < 1.0:
                return None, True

        wall = max(elapsed, sail_elapsed)
        sog_eff = last_bsp * (sail_elapsed / wall) if wall > 0 else last_bsp
        ctw = heading + self.boat.leeway_offset(last_tws, last_bsp, last_twd, heading)
        vx, vy = vector(norm360(ctw), last_bsp)
        _, cog = magnitude_bearing(vx, vy)
        n_man = node.n_maneuvers + (1 if maneuver in ("tack", "gybe") else 0)
        depth_m = (
            self.geo.depth.depth((lat, lon)) if self.geo.depth is not None else 0.0
        )

        child = Node(
            lat=lat,
            lon=lon,
            t=node.t + timedelta(seconds=wall),
            parent=node,
            heading=heading,
            bsp=last_bsp,
            tws=last_tws,
            twd=last_twd,
            twa=last_twa,
            sog=sog_eff,
            cog=cog,
            maneuver=maneuver,
            n_maneuvers=n_man,
            depth_m=depth_m,
            hazard_cost=0.0,
            rethink=need_rethink,
            guide_idx=guide_idx,
        )
        return child, geo_hit

    def _expand_frontier(
        self,
        frontier: list[Node],
        tgt: tuple[float, float],
        max_commit_s: float,
        arrive_nm: float,
    ) -> tuple[list[Node], int, int]:
        nxt: list[Node] = []
        n_tried = 0
        n_geo_hit = 0
        for node in frontier:
            w = self.wind.wind_at(node.lat, node.lon, node.t)
            if w is None:
                continue
            tws, twd = w
            up_twa = self.boat.target_upwind_twa(tws)
            beat_bsp = self.boat.bsp(tws, up_twa)
            bsp_floor = (
                self.p.min_bsp_fraction_of_beat * beat_bsp if beat_bsp > 0 else 0.0
            )

            for heading in self._candidate_headings(node, tws, twd, tgt, arrive_nm):
                n_tried += 1
                twa = twa_from(heading, twd)
                bsp = self.boat.bsp(tws, twa)
                if bsp <= 0:
                    continue
                if bsp_floor > 0 and bsp < bsp_floor:
                    continue
                child, geo_hit = self._commit_heading(
                    node,
                    heading,
                    tgt,
                    max_commit_s=max_commit_s,
                    arrive_nm=arrive_nm,
                    tws0=tws,
                    twd0=twd,
                )
                if geo_hit and child is None:
                    n_geo_hit += 1
                if child is not None:
                    if geo_hit:
                        n_geo_hit += 1
                    nxt.append(child)
        return nxt, n_tried, n_geo_hit

    @staticmethod
    def _arrivals_crossing_mark(children: list[Node], target: Mark) -> list[Node]:
        tgt = target.position
        radius = target.radius_nm
        found: list[Node] = []
        for child in children:
            parent = child.parent
            if parent is None:
                continue
            if distance_nm(parent.pos, tgt) <= radius:
                continue
            if distance_nm(child.pos, tgt) <= radius:
                found.append(child)
                continue
            for k in range(1, 8):
                f = k / 8.0
                lat = parent.lat + (child.lat - parent.lat) * f
                lon = parent.lon + (child.lon - parent.lon) * f
                if distance_nm((lat, lon), tgt) <= radius:
                    found.append(
                        Node(
                            lat=lat,
                            lon=lon,
                            t=parent.t
                            + timedelta(
                                seconds=(child.t - parent.t).total_seconds() * f
                            ),
                            parent=parent,
                            heading=child.heading,
                            bsp=child.bsp,
                            tws=child.tws,
                            twd=child.twd,
                            twa=child.twa,
                            sog=child.sog,
                            cog=child.cog,
                            maneuver=child.maneuver,
                            n_maneuvers=child.n_maneuvers,
                            depth_m=child.depth_m,
                            hazard_cost=0.0,
                            guide_idx=child.guide_idx,
                        )
                    )
                    break
        return found

    def _guide_blocked(self, node: Node, tgt: tuple[float, float]) -> bool:
        """True when land/shoal sits between the boat and its current aim point."""
        if self.geo.blocked_segment(node.pos, tgt):
            return True
        if self._chain is None or not self._guide_pts:
            return False
        guide = self._guide(node)
        return self.geo.blocked_segment(node.pos, guide)

    def _path_is_thrashing(self, node: Node, tgt: tuple[float, float]) -> bool:
        """True when the lead path is scribbling in a pocket instead of making way.

        Detects the Bembridge / Solent birds-nest: many heading reversals inside a
        small patch while mark distance barely moves. A clean beat has reversals
        too, but they span miles — geographic diameter separates the two.

        Uses crow-flies distance to ``tgt`` (not chain ``_rem``) so ancestors
        from the previous leg — with a stale ``guide_idx`` — cannot IndexError.
        """
        # Final approach to the mark is allowed to be busy.
        if distance_nm(node.pos, tgt) < 1.0:
            return False
        pts: list[tuple[float, float]] = []
        headings: list[float] = []
        dists: list[float] = []
        cur: Node | None = node
        sailed = 0.0
        while cur is not None and len(pts) < 14:
            if cur.maneuver == "mark" and cur is not node:
                break
            pts.append(cur.pos)
            dists.append(distance_nm(cur.pos, tgt))
            if cur.heading is not None:
                headings.append(cur.heading)
            if cur.parent is not None:
                sailed += distance_nm(cur.parent.pos, cur.pos)
            cur = cur.parent
            if sailed >= 3.5:
                break
        if len(pts) < 8 or len(headings) < 6:
            return False
        # Diameter of the recent track. Keep this tight — a normal Solent beat
        # under oscillating wind flips boards every ~1 nm and must NOT match.
        lat0 = sum(p[0] for p in pts) / len(pts)
        lon0 = sum(p[1] for p in pts) / len(pts)
        radius = max(distance_nm((lat0, lon0), p) for p in pts)
        if radius > 0.55:
            return False
        flips = 0
        for a, b in zip(headings, headings[1:]):
            if abs(signed_diff(a, b)) >= 90.0:
                flips += 1
        # dists[0]=lead, dists[-1]=oldest; positive gain = closed on mark.
        dist_gain = dists[-1] - dists[0]
        # Thrash: birds-nest scribble, several reverse flips, little progress.
        if flips >= 4 and dist_gain < 0.25:
            return True
        maneuvers = 0
        cur = node
        walked = 0.0
        while cur is not None and walked < 1.5:
            if cur.maneuver in ("tack", "gybe"):
                maneuvers += 1
            if cur.parent is None or cur.maneuver == "mark":
                break
            walked += distance_nm(cur.parent.pos, cur.pos)
            cur = cur.parent
        return maneuvers >= 5 and radius <= 0.55

    def _last_good_ancestor(
        self, node: Node, tgt: tuple[float, float]
    ) -> Node:
        """Walk back out of the thrash pocket to a node with room to rethink."""
        cluster = [node.pos]
        cur: Node | None = node
        best = node
        sailed = 0.0
        d_lead = distance_nm(node.pos, tgt)
        while cur is not None and cur.parent is not None:
            if cur.parent.maneuver == "mark":
                break
            sailed += distance_nm(cur.parent.pos, cur.pos)
            cur = cur.parent
            cluster.append(cur.pos)
            lat0 = sum(p[0] for p in cluster) / len(cluster)
            lon0 = sum(p[1] for p in cluster) / len(cluster)
            if max(distance_nm((lat0, lon0), p) for p in cluster) > 0.85:
                best = cur
                break
            if sailed >= 2.5:
                best = cur
                break
            # Prefer a node that was closer to the mark before the scribble.
            if distance_nm(cur.pos, tgt) + 0.15 < d_lead:
                best = cur
        return best

    def _recover_from_thrash(
        self,
        frontier: list[Node],
        tgt: tuple[float, float],
        radius_nm: float,
    ) -> list[Node]:
        """Backtrack out of a thrash pocket and commit the second-best escape.

        Drops the heading board that produced the scribble and retries from the
        last known-good ancestor so the frontier can leave the shoal contour.
        """
        lead = min(frontier, key=lambda n: self._rem(n, tgt))
        good = self._last_good_ancestor(lead, tgt)
        banned = lead.heading
        # Also ban the parent's board — thrash is usually a two-heading flip.
        banned2 = lead.parent.heading if lead.parent is not None else None
        rem0 = self._rem(good, tgt)
        w = self.wind.wind_at(good.lat, good.lon, good.t)
        if w is None:
            return []
        tws, twd = w
        up_twa = self.boat.target_upwind_twa(tws)
        dn_twa = self.boat.target_downwind_twa(tws)
        brg_aim = self._ban_aim_bearing(good, tgt)
        children: list[Node] = []
        for deg in range(0, 360, 15):
            heading = float(deg)
            if banned is not None and abs(signed_diff(heading, banned)) < 35.0:
                continue
            if banned2 is not None and abs(signed_diff(heading, banned2)) < 35.0:
                continue
            twa = twa_from(heading, twd)
            if twa < up_twa - 1.0 or twa > min(175.0, dn_twa + 25.0):
                continue
            if not self._probe_clear(good.pos, heading, probe_nm=0.8, tws=tws, twd=twd):
                continue
            end = destination(good.pos, heading, 0.8)
            trial = Node(
                lat=end[0],
                lon=end[1],
                t=good.t,
                heading=heading,
                guide_idx=good.guide_idx,
            )
            if self._rem(trial, tgt) > rem0 + float(self.p.progress_rem_slack_nm) + 0.4:
                continue
            child, _geo = self._commit_heading(
                good,
                heading,
                tgt,
                max_commit_s=min(900.0, float(self.p.max_commit_s)),
                tws0=tws,
                twd0=twd,
                arrive_nm=radius_nm,
            )
            if child is not None:
                children.append(child)
        if not children:
            return []
        children.sort(
            key=lambda n: (
                self._rem(n, tgt),
                -(self.geo.depth.depth(n.pos) if self.geo.depth is not None else 0.0),
                abs(signed_diff(n.heading or 0.0, brg_aim)),
            )
        )
        # Prefer the second-best when the best is still near the thrash patch.
        pick = children[0]
        if len(children) > 1 and distance_nm(pick.pos, lead.pos) < 0.5:
            pick = children[1]
        elif len(children) > 1:
            # Explicit second-best if best is on a near-banned residual board.
            if banned is not None and abs(signed_diff(pick.heading or 0.0, banned)) < 50.0:
                pick = children[1]
        logger.info(
            "thrash recover from=(%.4f,%.4f) good=(%.4f,%.4f) banned=%.0f → h=%.0f rem %.2f→%.2f",
            lead.lat,
            lead.lon,
            good.lat,
            good.lon,
            banned or -1.0,
            pick.heading or -1.0,
            rem0,
            self._rem(pick, tgt),
        )
        # Keep non-thrashing frontier peers so we don't collapse diversity.
        kept = [n for n in frontier if not self._path_is_thrashing(n, tgt)]
        return [pick, *kept[:6]]

    def _escape_frontier(
        self,
        frontier: list[Node],
        tgt: tuple[float, float],
        radius_nm: float,
    ) -> list[Node]:
        """Sail into clear water when the mark/guide line is blocked and progress stopped.

        The normal fan keeps aiming at the mark and the stall counter then gives
        up. A short step toward clear water that reduces chain rem is how you
        leave a shoal pocket or the wrong side of a headland.
        """
        node = min(frontier, key=lambda n: self._rem(n, tgt))
        w = self.wind.wind_at(node.lat, node.lon, node.t)
        if w is None:
            return []
        tws, twd = w
        up_twa = self.boat.target_upwind_twa(tws)
        dn_twa = self.boat.target_downwind_twa(tws)
        rem0 = self._rem(node, tgt)
        brg_aim = self._ban_aim_bearing(node, tgt)
        aim_twa = twa_from(brg_aim, twd)
        dist0 = distance_nm(node.pos, tgt)
        lockout_nm = max(0.5, float(self.p.tack_lockout_nm))
        fresh = (
            node.heading is not None
            and self._sailed_since_tack_nm(node) < lockout_nm
        )
        shore = max(0.5, float(self.p.tack_shore_probe_nm))
        held_into_shore = node.heading is None or not self._probe_clear(
            node.pos, node.heading, probe_nm=shore, tws=tws, twd=twd
        )
        if (
            not held_into_shore
            and node.heading is not None
            and self._held_board_deadends_sooner(
                node.pos, node.heading, tgt, tws, twd, radius_nm
            )
        ):
            held_into_shore = True
        parent = None
        if fresh:
            parent = Node(
                lat=node.lat,
                lon=node.lon,
                t=node.t,
                heading=node.heading,
                twd=node.twd,
            )
        children: list[Node] = []
        for h in range(0, 360, 20):
            heading = float(h)
            twa = twa_from(heading, twd)
            if not (up_twa <= twa <= min(175.0, dn_twa + 25.0)):
                continue
            # Same anti-spike rules as _candidate_headings — escape used to
            # bypass them and inject the Solent NNE gybe.
            if abs(signed_diff(heading, brg_aim)) > 90.0 and twa >= 90.0:
                continue
            if aim_twa < 90.0 and twa >= 90.0:
                continue
            if not self._probe_clear(node.pos, heading, probe_nm=0.8, tws=tws, twd=twd):
                continue
            # Prefer chain-rem progress; if the guide is stale, fall back to
            # closing distance on the race mark (Needles pocket).
            end = destination(node.pos, heading, 0.8)
            trial = Node(
                lat=end[0], lon=end[1], t=node.t, heading=heading, guide_idx=node.guide_idx
            )
            rem_ok = self._rem(trial, tgt) <= rem0 + float(self.p.progress_rem_slack_nm)
            mark_ok = distance_nm(end, tgt) <= dist0 + float(self.p.progress_rem_slack_nm)
            if not rem_ok and not mark_ok:
                continue
            if parent is not None and _detect_maneuver(
                parent, heading, twd, twa
            ) != "none":
                # Under lockout: lay, reverse-recovery, or held board into shore.
                if not self._heading_fetches(node.pos, heading, tgt, radius_nm):
                    reverse = abs(signed_diff(node.heading, brg_aim)) > 50.0
                    recovering = abs(signed_diff(heading, brg_aim)) <= 50.0
                    if not held_into_shore and not (reverse and recovering):
                        continue
            child, _geo = self._commit_heading(
                node,
                heading,
                tgt,
                max_commit_s=500.0,
                tws0=tws,
                twd0=twd,
                arrive_nm=radius_nm,
            )
            if child is not None:
                children.append(child)
        if not children:
            return []
        children.sort(
            key=lambda n: (
                self._rem(n, tgt),
                -(self.geo.depth.depth(n.pos) if self.geo.depth is not None else 0.0),
            )
        )
        return children[:4]

    def _expand_leg(
        self,
        frontier: list[Node],
        target: Mark,
        *,
        leg_index: int = 0,
        leg_total: int = 1,
        emit_start: bool = True,
    ) -> tuple[Node | None, bool, list[Node]]:
        tgt = target.position
        origin = frontier[0].pos
        max_commit = self.p.max_commit_s
        for n in frontier:
            n.guide_idx = 0
        self._begin_guidance(origin, tgt)
        best_remaining = min(self._rem(n, tgt) for n in frontier)
        initial_remaining = best_remaining
        stall_limit = max(
            self.p.no_progress_steps,
            int(initial_remaining * self.p.stall_rem_factor) + self.p.stall_rem_offset,
        )
        stalled = 0
        self._escapes_left = 6
        thrash_left = 4
        self._careful = 0
        self._geo_hit_streak = 0
        # Restore opportunistic segment sampling for the new leg.
        self.geo.segment_sample_nm = 0.05
        t_leg = time.perf_counter()
        n_wind = 0
        n_blocked = 0
        n_expansions = 0
        steps_taken = 0
        termination = "max_steps"
        arrival: Node | None = None
        exhausted = False

        name = target.name or f"mark-{leg_index + 1}"
        if emit_start:
            self._emit(
                {
                    "type": "leg",
                    "model_id": self.model_id,
                    "index": leg_index,
                    "total": leg_total,
                    "name": name,
                    "status": "start",
                }
            )

        for _ in range(self.p.max_steps_per_leg):
            steps_taken += 1
            commit_now = max_commit
            rem_now = min(self._rem(n, tgt) for n in frontier)
            if rem_now < self.p.near_mark_nm:
                commit_now = min(
                    commit_now,
                    float(max(self.p.short_commit_min_s, self.p.time_step_s // 2)),
                )

            nxt, n_tried, n_geo_hit = self._expand_frontier(
                frontier, tgt, commit_now, target.radius_nm
            )
            n_wind += len(frontier)
            n_blocked += n_geo_hit
            n_expansions += len(nxt)

            # Geo-hit streak → stretch probes (fast mode was lying about clearance).
            if n_tried > 0 and n_geo_hit / max(n_tried, 1) > self.p.geo_hit_retry_fraction:
                self._geo_hit_streak += 1
                if self._geo_hit_streak >= 3:
                    self._escalate_careful("geo_hit_streak")
                    self._geo_hit_streak = 0
            else:
                self._geo_hit_streak = 0

            # Adaptive: most candidates geo-hit → retry with shorter commits.
            if n_tried > 0 and n_geo_hit / n_tried > self.p.geo_hit_retry_fraction and nxt:
                short = float(max(self.p.short_commit_min_s, self.p.time_step_s // 2))
                if short < commit_now:
                    nxt2, _, hit2 = self._expand_frontier(
                        frontier, tgt, short, target.radius_nm
                    )
                    n_blocked += hit2
                    if len(nxt2) > len(nxt):
                        nxt = nxt2
                        commit_now = short
                        n_expansions += len(nxt2)

            any_wind = n_tried > 0 or bool(nxt)
            if not any_wind and not nxt:
                w0 = self.wind.wind_at(frontier[0].lat, frontier[0].lon, frontier[0].t)
                if w0 is None:
                    termination = "forecast_exhausted"
                    exhausted = True
                    arrival = None
                    break

            # Empty-frontier retry: same progressive-from-mark fan, shorter commit.
            if not nxt:
                short = float(max(self.p.short_commit_min_s, self.p.time_step_s // 2))
                nxt, _, hit_r = self._expand_frontier(
                    frontier, tgt, short, target.radius_nm
                )
                n_blocked += hit_r
                n_expansions += len(nxt)

            if not nxt:
                creep = float(max(self.p.creep_commit_min_s, self.p.time_step_s // 4))
                nxt, _, hit_r = self._expand_frontier(
                    frontier, tgt, creep, target.radius_nm
                )
                n_blocked += hit_r
                n_expansions += len(nxt)

            if not nxt and self._escapes_left > 0 and frontier:
                lead = min(frontier, key=lambda n: self._rem(n, tgt))
                if self._guide_blocked(lead, tgt):
                    escaped = self._escape_frontier(frontier, tgt, target.radius_nm)
                    if escaped:
                        self._escapes_left -= 1
                        frontier = escaped
                        stalled = 0
                        continue
            if not nxt:
                # Wind cliff: nodes still have wind_at(t) but not enough horizon
                # left to take a step — ask /route to extend the forecast.
                min_need = float(max(self.p.creep_commit_min_s, self.p.time_step_s // 4))
                wind_cliff = True
                for n in frontier:
                    if self.wind.wind_at(n.lat, n.lon, n.t) is None:
                        continue
                    left = _seconds_of_wind_remaining(self.wind, n.t)
                    if left is None or left >= min_need:
                        wind_cliff = False
                        break
                if wind_cliff:
                    termination = "forecast_exhausted"
                    exhausted = True
                    arrival = None
                    break
                self._unreachable = True
                termination = "frontier_empty"
                self._leg_warnings.append(
                    f"mark '{name}' unreachable (frontier empty after {steps_taken} steps)"
                )
                arrival = None
                break

            crossed = self._arrivals_crossing_mark(nxt, target)
            frontier = self._prune(nxt, tgt, origin)
            if not frontier and not crossed:
                self._unreachable = True
                termination = "frontier_empty"
                self._leg_warnings.append(
                    f"mark '{name}' unreachable (frontier empty after {steps_taken} steps)"
                )
                arrival = None
                break

            # A few hundredths of a mile outside the circle is already the finish.
            # Tacking to clip the exact radius records a tack of a boat-length.
            arrivals = [
                n
                for n in frontier
                if distance_nm(n.pos, tgt) <= target.radius_nm + 0.03
            ]
            arrivals.extend(crossed)
            # Near-mark stall: false depth holes (Fastnet Rock, Genoa breakwater)
            # leave rem stuck at 1–2 nm. After real stalling, accept the closest
            # boat inside half a mile rather than dying.
            if not arrivals and stalled >= max(15, stall_limit // 4):
                close = [
                    n for n in frontier if distance_nm(n.pos, tgt) <= max(0.5, target.radius_nm + 0.25)
                ]
                if close:
                    arrivals = close
            if arrivals:
                best = min(arrivals, key=lambda n: n.t)
                arrival = self._snap_to_mark(best, target)
                termination = "arrived"
                exhausted = False
                break

            rem = min(self._rem(n, tgt) for n in frontier)
            lead = min(frontier, key=lambda n: self._rem(n, tgt))
            # Shore-creep nests often "improve" rem by metres each step, so the
            # stall counter never fires — still detect the geographic scribble.
            if (
                thrash_left > 0
                and steps_taken >= 10
                and steps_taken % 5 == 0
                and self._path_is_thrashing(lead, tgt)
            ):
                # Stretch probes first; only rewrite the frontier if recover
                # actually improves rem. False positives under oscillating wind
                # used to pick a reverse board and empty the Needles approach.
                self._escalate_careful("thrash")
                recovered = self._recover_from_thrash(
                    frontier, tgt, target.radius_nm
                )
                if recovered and self._rem(recovered[0], tgt) < rem - 0.15:
                    thrash_left -= 1
                    frontier = recovered
                    stalled = 0
                    continue
            if rem < best_remaining - 1e-4:
                best_remaining = rem
                stalled = 0
            else:
                stalled += 1
                if (
                    thrash_left > 0
                    and stalled >= 8
                    and self._path_is_thrashing(lead, tgt)
                ):
                    self._escalate_careful("thrash_stall")
                    recovered = self._recover_from_thrash(
                        frontier, tgt, target.radius_nm
                    )
                    if recovered and self._rem(recovered[0], tgt) < rem - 0.15:
                        thrash_left -= 1
                        frontier = recovered
                        stalled = 0
                        continue
                if (
                    stalled == stall_limit // 2
                    and self._escapes_left > 0
                    and self._guide_blocked(lead, tgt)
                ):
                    escaped = self._escape_frontier(frontier, tgt, target.radius_nm)
                    if escaped:
                        self._escapes_left -= 1
                        frontier = escaped
                        stalled = 0
                        continue
                if stalled >= stall_limit:
                    self._unreachable = True
                    termination = "no_progress"
                    self._leg_warnings.append(
                        f"mark '{name}' unreachable "
                        f"(no progress for {stalled} steps, rem={rem:.2f}nm)"
                    )
                    arrival = None
                    break
        else:
            self._unreachable = True
            termination = "max_steps"
            self._leg_warnings.append(
                f"mark '{name}' unreachable (max steps exceeded)"
            )
            arrival = None

        ms = (time.perf_counter() - t_leg) * 1000
        logger.info(
            "leg %s/%s '%s' %s steps=%d wind_at=%d blocked=%d expansions=%d "
            "frontier=%d rem=%.2f/%.2fnm stall_limit=%d %.0fms",
            leg_index + 1,
            leg_total,
            name,
            termination,
            steps_taken,
            n_wind,
            n_blocked,
            n_expansions,
            len(frontier) if frontier else 0,
            best_remaining,
            initial_remaining,
            stall_limit,
            ms,
        )
        if not (arrival is None and exhausted):
            status = "done" if arrival is not None else "error"
            self._emit(
                {
                    "type": "leg",
                    "model_id": self.model_id,
                    "index": leg_index,
                    "total": leg_total,
                    "name": name,
                    "status": status,
                    "ms": round(ms, 1),
                    "termination": termination,
                    "steps": steps_taken,
                    "wind_at_calls": n_wind,
                    "blocked_calls": n_blocked,
                    "expansions": n_expansions,
                }
            )
        return arrival, exhausted, frontier

    @staticmethod
    def _snap_to_mark(node: Node, target: Mark) -> Node:
        mark_lat, mark_lon = target.position
        # Preserve sailing heading; parent=arrival so the approach stays in the chain.
        brg = (
            node.heading
            if node.heading is not None
            else (
                bearing(node.parent.pos, target.position)
                if node.parent is not None
                else 0.0
            )
        )
        # If arrival is already essentially on the mark, skip duplicate node.
        parent = node
        if distance_nm(node.pos, target.position) < 1e-4 and node.parent is not None:
            parent = node.parent
        return Node(
            lat=mark_lat,
            lon=mark_lon,
            t=node.t,
            parent=parent,
            heading=brg,
            bsp=node.bsp,
            tws=node.tws,
            twd=node.twd,
            twa=node.twa,
            sog=node.sog,
            cog=brg,
            maneuver="mark",
            n_maneuvers=node.n_maneuvers,
            depth_m=node.depth_m,
            hazard_cost=0.0,
            rethink=True,  # next leg must re-seed from the new mark bearing
        )

    def route(self, course: Course, model_id: str, *, resume: bool = False) -> ModelRoute:
        self.model_id = model_id
        depart = course.depart_utc
        if not resume:
            marks_s = ", ".join(
                f"{m.name or i}=({m.position[0]:.4f},{m.position[1]:.4f})"
                for i, m in enumerate(course.marks, start=1)
            )
            logger.info(
                "route %s start=(%.4f,%.4f) finish=(%.4f,%.4f) marks=[%s] depart=%s",
                model_id,
                course.start[0],
                course.start[1],
                course.finish[0],
                course.finish[1],
                marks_s,
                depart.isoformat(),
            )

        if resume and self._pause is not None:
            pause = self._pause
            self._pause = None
            start_node = pause.start_node
            last_arrival: Node | None = pause.last_arrival
            frontier = list(pause.frontier)
            targets = pause.targets
            start_leg = pause.leg_index
            self._leg_warnings = list(pause.leg_warnings)
            emit_start_first = False
        else:
            self._pause = None
            start_depth = (
                self.geo.depth.depth(course.start) if self.geo.depth is not None else 0.0
            )
            start_node = Node(
                lat=course.start[0],
                lon=course.start[1],
                t=depart,
                depth_m=start_depth,
                hazard_cost=0.0,
            )
            targets = list(course.marks)
            targets.append(
                Mark(
                    position=course.finish,
                    radius_nm=self.p.finish_snap_radius_nm,
                    rounding="either",
                    name="finish",
                )
            )
            frontier = [start_node]
            last_arrival = start_node
            start_leg = 0
            self._leg_warnings = []
            self._route_waypoints = []
            emit_start_first = True

        exhausted = False
        self._unreachable = False
        leg_total = len(targets)

        for i in range(start_leg, leg_total):
            target = targets[i]
            arrival, exh, frontier_out = self._expand_leg(
                frontier,
                target,
                leg_index=i,
                leg_total=leg_total,
                emit_start=emit_start_first if i == start_leg else True,
            )
            if arrival is None:
                exhausted = exh
                # Forecast pause is resumable — not a hard unreachable yet.
                self._unreachable = not exh
                if exh and frontier_out:
                    self._pause = _PauseState(
                        start_node=start_node,
                        last_arrival=last_arrival if last_arrival is not None else start_node,
                        frontier=list(frontier_out),
                        leg_index=i,
                        targets=targets,
                        leg_warnings=list(self._leg_warnings),
                    )
                break
            last_arrival = arrival
            frontier = [arrival]

        legs = self._backtrack(start_node, last_arrival)
        n_tacks = sum(1 for leg in legs if leg.maneuver == "tack")
        n_gybes = sum(1 for leg in legs if leg.maneuver == "gybe")
        dist = sum(
            distance_nm(legs[i - 1].position, legs[i].position) for i in range(1, len(legs))
        )
        eta = legs[-1].t_utc if legs else depart
        elapsed = int((eta - depart).total_seconds())

        return ModelRoute(
            model_id=model_id,
            legs=legs,
            eta_utc=eta,
            elapsed_s=elapsed,
            distance_nm=round(dist, 2),
            n_tacks=n_tacks,
            n_gybes=n_gybes,
            forecast_exhausted=exhausted,
            unreachable=self._unreachable,
            warnings=list(self._leg_warnings),
            data_source={
                "depth": "region_depth.tif",
                "land": "osm_land_chart",
            },
            waypoints=list(self._route_waypoints) or None,
        )

    @staticmethod
    def _backtrack(start: Node, end: Node | None) -> list[RouteLeg]:
        if end is None:
            end = start
        chain: list[Node] = []
        n: Node | None = end
        while n is not None:
            chain.append(n)
            n = n.parent
        chain.reverse()
        legs: list[RouteLeg] = []
        for node in chain:
            legs.append(
                RouteLeg(
                    t_utc=node.t,
                    position=(node.lat, node.lon),
                    tws=round(node.tws, 2),
                    twd=round(node.twd, 1),
                    twa=round(node.twa, 1),
                    heading=round(node.heading if node.heading is not None else 0.0, 1),
                    bsp=round(node.bsp, 2),
                    sog=round(node.sog, 2),
                    cog=round(node.cog, 1),
                    maneuver=node.maneuver if node.maneuver in ("none", "tack", "gybe", "mark") else "none",  # type: ignore[arg-type]
                    depth_m=round(node.depth_m, 2),
                    hazard_cost=0.0,
                )
            )
        return legs
