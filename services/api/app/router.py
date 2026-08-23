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
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .boat import Boat
from .geo import GeoContext, bearing, destination, distance_nm
from .metocean import WindField
from .navmath import magnitude_bearing, norm360, signed_diff, twa_from, vector
from .schemas import Course, Mark, ModelRoute, RouteLeg

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None]


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
    target: tuple[float, float],
    maneuver_nm: float,
    max_maneuvers: int,
    overflow_nm: float,
    p: RouterParams,
) -> float:
    """Lower is better: ETA proxy (time + rem/bsp) + soft maneuver costs."""
    rem = _remaining_nm(node, target)
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

    @property
    def can_resume(self) -> bool:
        return self._pause is not None

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_progress is not None:
            self.on_progress(event)

    def _probe_clear(
        self,
        pos: tuple[float, float],
        heading: float,
        *,
        probe_nm: float,
        tws: float | None = None,
        twd: float | None = None,
    ) -> bool:
        """True if a short step on heading (with leeway if known) is navigable."""
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
        return not (
            self.geo.blocked_point(probe) or self.geo.blocked_segment(pos, probe)
        )

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
        always_extra: set[float] | None = None,
    ) -> list[float]:
        """Widen ±heading_step from mark seed until a clear sailable heading exists.

        Stops at the first clear ring. Hard cap is ±180° from seed — enough to
        circumnavigate land between boat and mark without a separate fan mode.
        """
        step = self.p.heading_step_deg
        probe_nm = min(
            self.p.fan_probe_nm_max,
            max(self.p.mark_probe_nm * self.p.fan_probe_mark_frac, self.p.fan_probe_nm_min),
        )

        found: list[float] = []
        seen: set[float] = set()

        def consider(h: float) -> bool:
            key = round(norm360(h), 3)
            if key in seen:
                return False
            twa = twa_from(h, twd)
            if not (up_twa <= twa <= dn_twa):
                return False
            if not self._probe_clear(pos, h, probe_nm=probe_nm, tws=tws, twd=twd):
                return False
            seen.add(key)
            found.append(norm360(h))
            return True

        if always_extra:
            for h in always_extra:
                consider(h)

        if consider(seed):
            return list(dict.fromkeys(found))

        half = step
        while half <= self.p.fan_half_max_deg + 1e-6:
            hit = consider(norm360(seed + half))
            hit = consider(norm360(seed - half)) or hit
            if hit:
                break
            half += step

        if not found:
            found = [seed]
            if always_extra:
                found.extend(always_extra)
        return list(dict.fromkeys(found))

    def _seed_heading(
        self,
        node: Node,
        tws: float,
        twd: float,
        target: tuple[float, float],
    ) -> tuple[float, str, float]:
        """Return (seed_heading, mode, twa_brg).

        mode is ``up_vmg`` / ``dn_vmg`` / ``bearing``.
        """
        brg = bearing(node.pos, target)
        up_twa = self.boat.target_upwind_twa(tws)
        dn_twa = self.boat.target_downwind_twa(tws)
        port_up = norm360(twd + up_twa)
        stbd_up = norm360(twd - up_twa)
        port_dn = norm360(twd + dn_twa)
        stbd_dn = norm360(twd - dn_twa)
        twa_brg = twa_from(brg, twd)

        def _hysteresis(closer: float, port: float, stbd: float) -> float:
            """Stay on current side's VMG unless opposite is clearly closer to mark."""
            if node.heading is None or node.rethink:
                return closer
            same = (
                port
                if abs(signed_diff(port, node.heading))
                <= abs(signed_diff(stbd, node.heading))
                else stbd
            )
            rem_h = distance_nm(node.pos, target)
            # Near mark: never flip seed — finish the approach tack/gybe.
            if rem_h < self.p.near_mark_nm:
                return same
            if abs(signed_diff(closer, brg)) + self.p.vmg_hysteresis_deg < abs(
                signed_diff(same, brg)
            ):
                return closer
            return same

        if twa_brg < up_twa:
            closer = (
                port_up
                if abs(signed_diff(port_up, brg)) <= abs(signed_diff(stbd_up, brg))
                else stbd_up
            )
            return _hysteresis(closer, port_up, stbd_up), "up_vmg", twa_brg
        if twa_brg > dn_twa:
            closer = (
                port_dn
                if abs(signed_diff(port_dn, brg)) <= abs(signed_diff(stbd_dn, brg))
                else stbd_dn
            )
            return _hysteresis(closer, port_dn, stbd_dn), "dn_vmg", twa_brg
        return brg, "bearing", twa_brg

    def _candidate_headings(
        self,
        node: Node,
        tws: float,
        twd: float,
        target: tuple[float, float],
    ) -> list[float]:
        """Seed mark/VMG → progressive fan → gated sticky parent / opposite VMG."""
        brg = bearing(node.pos, target)
        up_twa = self.boat.target_upwind_twa(tws)
        dn_twa = self.boat.target_downwind_twa(tws)
        port_up = norm360(twd + up_twa)
        stbd_up = norm360(twd - up_twa)
        port_dn = norm360(twd + dn_twa)
        stbd_dn = norm360(twd - dn_twa)
        seed, seed_mode, twa_brg = self._seed_heading(node, tws, twd, target)

        mark_clear = self._mark_bearing_clear(node.pos, target, tws=tws, twd=twd)
        extras: set[float] = set()
        # Sticky current heading only while continuing a leg — never right after
        # a mark snap (inbound heading points at the *previous* mark).
        if (
            node.heading is not None
            and not node.rethink
            and node.maneuver != "mark"
            and not mark_clear
        ):
            extras.add(node.heading)
        # Corridor: after geo rethink, keep prior heading while mark still blocked.
        if node.heading is not None and node.rethink and not mark_clear:
            extras.add(node.heading)

        heads = set(
            self._progressive_fan_from_seed(
                node.pos,
                seed,
                tws,
                twd,
                up_twa,
                dn_twa,
                always_extra=extras or None,
            )
        )
        heads.add(seed)

        # Open-water diversity: when pointing at the mark is the seed and clear,
        # offer ±one step so prune can prefer a slightly hotter/cooler angle.
        if seed_mode == "bearing" and mark_clear:
            step = self.p.heading_step_deg
            heads.add(norm360(seed + step))
            heads.add(norm360(seed - step))

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
        heads.add(same_up)
        if twa_brg >= self.p.downwind_twa_gate_deg:
            heads.add(same_dn)

        rem = distance_nm(node.pos, target)
        course_err = abs(signed_diff(brg, ref))
        can_point = twa_brg >= up_twa
        can_run = twa_brg <= dn_twa
        # Opposite-tack only while we cannot point at the mark, not on final approach.
        # Use 90° (abeam) so we don't tack just because the mark is ~70° off the bow.
        if (
            not can_point
            and rem >= self.p.near_mark_nm
            and (
                course_err > self.p.opposite_maneuver_course_err_deg
                or (
                    node.heading is not None
                    and twa_from(node.heading, twd)
                    < up_twa - self.p.opposite_tack_twa_slack_deg
                )
                or abs(signed_diff(same_up, ref))
                > self.p.opposite_maneuver_heading_spread_deg
            )
        ):
            heads.add(opp_up)
        # Opposite gybe: mirror of opposite tack when mark is deeper than DN VMG.
        if (
            not can_run
            and rem >= self.p.near_mark_nm
            and (
                course_err > self.p.opposite_maneuver_course_err_deg
                or (
                    node.heading is not None
                    and twa_from(node.heading, twd)
                    > dn_twa + self.p.opposite_gybe_twa_slack_deg
                )
                or abs(signed_diff(same_dn, ref))
                > self.p.opposite_maneuver_heading_spread_deg
            )
        ):
            heads.add(opp_dn)

        out: list[float] = []
        forced = {seed, same_up, same_dn, opp_up, opp_dn} | extras
        for h in heads:
            twa = twa_from(h, twd)
            if h in forced or up_twa <= twa <= dn_twa:
                out.append(h)
        return list(dict.fromkeys(out))

    def _score(self, node: Node, target: tuple[float, float]) -> float:
        return _prune_score(
            node,
            target,
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
        best_dmg = min(nodes, key=lambda n: _remaining_nm(n, target))
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

    def _last_legal_point(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> tuple[float, float] | None:
        if not self.geo.blocked_segment(start, end) and not self.geo.blocked_point(end):
            return end
        if self.geo.blocked_point(start):
            return None
        lo_lat, lo_lon = start
        hi_lat, hi_lon = end
        last_ok: tuple[float, float] | None = start
        for _ in range(10):
            mid = ((lo_lat + hi_lat) * 0.5, (lo_lon + hi_lon) * 0.5)
            if self.geo.blocked_segment(start, mid) or self.geo.blocked_point(mid):
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
        """Stop escape/beat commit when mark is free again or past layline."""
        brg = bearing(pos, tgt)
        if abs(signed_diff(heading, brg)) < self.p.rethink_heading_vs_bearing_deg:
            return False
        up_twa = self.boat.target_upwind_twa(tws)
        can_point = twa_from(brg, twd) >= up_twa
        mark_clear = self._mark_bearing_clear(pos, tgt, tws=tws, twd=twd)
        if mark_clear and can_point:
            return True
        if can_point and abs(signed_diff(brg, heading)) > self.p.layline_stop_deg:
            return True
        if abs(signed_diff(brg, heading)) > self.p.past_layline_deg:
            return True
        return False

    def _commit_heading(
        self,
        node: Node,
        heading: float,
        tgt: tuple[float, float],
        *,
        max_commit_s: float,
        tws0: float,
        twd0: float,
    ) -> tuple[Node | None, bool]:
        """Hold heading until rethink (mark clear / layline / wind / geo) or cap."""
        twa0 = twa_from(heading, twd0)
        bsp0 = self.boat.bsp(tws0, twa0)
        if bsp0 <= 0:
            return None, False

        maneuver = _detect_maneuver(node, heading, twd0, twa0)
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
        rem0 = distance_nm(node.pos, tgt)
        if bsp0 > 0:
            budget = min(
                budget, (rem0 / bsp0) * 3600.0 + self.p.commit_arrival_buffer_s
            )
        # After a tack/gybe, re-evaluate soon so we don't overstand the new layline.
        if maneuver in ("tack", "gybe"):
            budget = min(budget, float(self.p.time_step_s))
        # Constraint mode: creep one substep while mark bearing is blocked.
        if not self._mark_bearing_clear(node.pos, tgt, tws=tws0, twd=twd0):
            budget = min(budget, float(self.p.time_step_s))
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

        while elapsed + 1e-6 < budget:
            dt = min(sub, budget - elapsed)
            sail_dt = dt
            if elapsed < 1e-9 and pen > 0:
                sail_dt = max(0.0, dt - pen)

            if elapsed > 1e-6 and self._should_rethink_heading(
                heading=heading,
                pos=(lat, lon),
                tgt=tgt,
                tws=last_tws,
                twd=last_twd,
            ):
                need_rethink = True
                break

            w = self.wind.wind_at(lat, lon, t + timedelta(seconds=dt * 0.5))
            if w is None:
                break
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
                break

            ctw = heading + self.boat.leeway_offset(tws, bsp, twd, heading)
            vx, vy = vector(norm360(ctw), bsp)
            nlat, nlon = _advance(lat, lon, vx, vy, sail_dt)
            if self.geo.blocked_segment((lat, lon), (nlat, nlon)) or self.geo.blocked_point(
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

            lat, lon = nlat, nlon
            elapsed += dt
            sail_elapsed += sail_dt
            t = node.t + timedelta(seconds=elapsed)
            last_tws, last_twd = tws, twd
            last_bsp, last_twa = bsp, twa

            if distance_nm((lat, lon), tgt) <= self.p.mark_arrival_break_nm:
                break

            if self._should_rethink_heading(
                heading=heading,
                pos=(lat, lon),
                tgt=tgt,
                tws=last_tws,
                twd=last_twd,
            ):
                need_rethink = True
                break

        if elapsed < 1.0 and sail_elapsed < 1.0:
            return None, geo_hit

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
        )
        return child, geo_hit

    def _expand_frontier(
        self,
        frontier: list[Node],
        tgt: tuple[float, float],
        max_commit_s: float,
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

            for heading in self._candidate_headings(node, tws, twd, tgt):
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
                        )
                    )
                    break
        return found

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
        best_remaining = min(_remaining_nm(n, tgt) for n in frontier)
        initial_remaining = best_remaining
        stall_limit = max(
            self.p.no_progress_steps,
            int(initial_remaining * self.p.stall_rem_factor) + self.p.stall_rem_offset,
        )
        stalled = 0
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
            rem_now = min(_remaining_nm(n, tgt) for n in frontier)
            if rem_now < self.p.near_mark_nm:
                commit_now = min(
                    commit_now,
                    float(max(self.p.short_commit_min_s, self.p.time_step_s // 2)),
                )

            nxt, n_tried, n_geo_hit = self._expand_frontier(
                frontier, tgt, commit_now
            )
            n_wind += len(frontier)
            n_blocked += n_geo_hit
            n_expansions += len(nxt)

            # Adaptive: most candidates geo-hit → retry with shorter commits.
            if n_tried > 0 and n_geo_hit / n_tried > self.p.geo_hit_retry_fraction and nxt:
                short = float(max(self.p.short_commit_min_s, self.p.time_step_s // 2))
                if short < commit_now:
                    nxt2, _, hit2 = self._expand_frontier(frontier, tgt, short)
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
                nxt, _, hit_r = self._expand_frontier(frontier, tgt, short)
                n_blocked += hit_r
                n_expansions += len(nxt)

            if not nxt:
                creep = float(max(self.p.creep_commit_min_s, self.p.time_step_s // 4))
                nxt, _, hit_r = self._expand_frontier(frontier, tgt, creep)
                n_blocked += hit_r
                n_expansions += len(nxt)

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

            arrivals = [
                n for n in frontier if distance_nm(n.pos, tgt) <= target.radius_nm
            ]
            arrivals.extend(crossed)
            if arrivals:
                best = min(arrivals, key=lambda n: n.t)
                arrival = self._snap_to_mark(best, target)
                termination = "arrived"
                exhausted = False
                break

            rem = min(_remaining_nm(n, tgt) for n in frontier)
            if rem < best_remaining - 1e-4:
                best_remaining = rem
                stalled = 0
            else:
                stalled += 1
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
                    position=(round(node.lat, 5), round(node.lon, 5)),
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
