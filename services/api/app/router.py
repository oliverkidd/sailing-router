"""Isochrone router: wind + leeway, one route per model.

The frontier is expanded in fixed time steps; at each step every live node fans out
over candidate headings (sailable mark bearing + polar VMG beat/run on both tacks +
heading fan, widened near blocked water), each producing a child node after applying
the polar, leeway, and the land/depth/exclusion masks. Candidates slower than a
fraction of beat-angle boat speed are skipped (no no-go pinching). The frontier is
pruned as a true earliest-time isochrone: bucket by bearing from the leg origin,
keep greatest distance-made-good toward the target with maneuver + heading-continuity
tiebreaks (no soft depth/shore cost — depth is a hard min+margin cutoff).
Then we backtrack parent pointers from the best finish node into a list of RouteLegs.
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
from .geo.hazard import HazardGrid
from .metocean import WindField
from .navmath import magnitude_bearing, norm360, signed_diff, twa_from, vector
from .schemas import Course, Mark, ModelRoute, RouteLeg

logger = logging.getLogger(__name__)

# Progress callback: receives event dicts (stage/leg) for SSE streaming.
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
    hazard_cost: float = 0.0

    @property
    def pos(self) -> tuple[float, float]:
        return (self.lat, self.lon)


@dataclass
class RouterParams:
    time_step_s: int = 300
    heading_step_deg: float = 7.5
    sector_width_deg: float = 5.0
    cluster_cell_deg: float = 0.03
    max_frontier: int = 3000
    max_steps_per_leg: int = 4000
    # Soft prune cost (nm) per tack/gybe — prefers long clean tacks over scribbles.
    maneuver_prune_nm: float = 0.55
    depth_safety_margin_m: float = 1.0
    # Base abort if remaining-to-target fails to improve; scaled up with leg length.
    no_progress_steps: int = 60
    # Soft preference only — nodes past this get a prune penalty, never hard-dropped.
    max_consecutive_maneuvers: int = 16
    # Extra prune nm per maneuver beyond max_consecutive_maneuvers (cove thrash soft-cap).
    maneuver_overflow_nm: float = 1.5
    # Skip candidates slower than this fraction of beat-angle boat speed (no-go crawl).
    min_bsp_fraction_of_beat: float = 0.4
    # Remaining distance (nm) below which we use a finer time step near the mark.
    near_mark_nm: float = 0.8
    # Soft prune nm when heading changes a lot vs parent (kills micro-wiggles).
    heading_continuity_nm: float = 0.08


def _wind_side(heading: float, twd: float) -> float:
    """+1 if wind is on one side of the heading, -1 the other, 0 head/dead on."""
    d = signed_diff(twd, heading)
    if abs(d) < 1e-6 or abs(abs(d) - 180.0) < 1e-6:
        return 0.0
    return 1.0 if d > 0 else -1.0


def _detect_maneuver(parent: Node, heading: float, twd: float, twa: float) -> str:
    """Label a wind-side flip as tack (upwind) or gybe (downwind).

    Every genuine side change is counted and penalised — no consecutive-suppression —
    so short-tacking pays its full time cost versus one long tack.
    """
    if parent.heading is None:
        return "none"
    ref_twd = parent.twd
    if _wind_side(parent.heading, ref_twd) == _wind_side(heading, ref_twd):
        return "none"
    return "tack" if twa < 90.0 else "gybe"


def _remaining_nm(node: Node, target: tuple[float, float]) -> float:
    """Great-circle distance left to the mark — lower is better (more DMG)."""
    return distance_nm(node.pos, target)


def _prune_score(
    node: Node,
    target: tuple[float, float],
    origin: tuple[float, float],
    maneuver_nm: float,
    max_maneuvers: int,
    overflow_nm: float,
    heading_continuity_nm: float = 0.0,
) -> float:
    """Lower is better: remaining-to-target + maneuver / heading continuity costs.

    Soft depth/shore hazard is intentionally NOT in the score — depth is a hard
    cutoff only. Maneuver + heading continuity prefer long clean tacks over
    scribble zigzags; overflow soft-caps cove thrashing.
    """
    overflow = max(0, node.n_maneuvers - max_maneuvers)
    heading_pen = 0.0
    if (
        heading_continuity_nm > 0
        and node.parent is not None
        and node.parent.heading is not None
        and node.heading is not None
        and node.maneuver == "none"
    ):
        # Penalise large course changes that are not labelled tack/gybe (micro-wiggles).
        err = abs(signed_diff(node.heading, node.parent.heading))
        if err > 20.0:
            heading_pen = heading_continuity_nm * ((err - 20.0) / 45.0)
    return (
        _remaining_nm(node, target)
        + maneuver_nm * node.n_maneuvers
        + overflow_nm * overflow
        + heading_pen
        - 0.01 * distance_nm(origin, node.pos)
    )


def _advance(lat: float, lon: float, gx: float, gy: float, dt_s: float) -> tuple[float, float]:
    """Integrate ground velocity (knots east/north) over dt_s seconds."""
    speed, brg = magnitude_bearing(gx, gy)
    dist = speed * (dt_s / 3600.0)
    if dist <= 0:
        return lat, lon
    return destination((lat, lon), brg, dist)


_AUTO_HAZARD = object()


class IsochroneRouter:
    def __init__(
        self,
        boat: Boat,
        wind: WindField,
        geo: GeoContext,
        params: RouterParams,
        hazard: HazardGrid | None | object = _AUTO_HAZARD,
        on_progress: ProgressCallback | None = None,
        model_id: str = "",
    ):
        self.boat = boat
        self.wind = wind
        self.geo = geo
        self.p = params
        self.on_progress = on_progress
        self.model_id = model_id
        # Apply safety margin to geo blocking for this route.
        self.geo.depth_safety_margin_m = params.depth_safety_margin_m
        # Soft hazard is disabled by product decision: depth is a hard cutoff only
        # (min_depth_m + safety margin). Passing an explicit HazardGrid still works
        # for tests that want soft costs.
        if hazard is _AUTO_HAZARD:
            self.hazard = None
        else:
            self.hazard = hazard  # type: ignore[assignment]
        self._fan_headings = [
            h * params.heading_step_deg
            for h in range(int(round(360.0 / params.heading_step_deg)))
        ]
        self._leg_warnings: list[str] = []
        self._unreachable = False

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_progress is not None:
            self.on_progress(event)

    def _candidate_headings(
        self,
        node: Node,
        tws: float,
        twd: float,
        target: tuple[float, float],
        *,
        fan_half_override: float | None = None,
    ) -> list[float]:
        """Sailable mark heading + VMG beat/run (both tacks) + fan near current.

        Never offers a raw mark bearing inside the no-go / deep-downwind band —
        that is what produced sub-1kt pinching legs. Layline headings come from
        ``boat.target_upwind_twa`` / ``target_downwind_twa`` (BoatConfig overrides
        or polar VMG). Opposite-tack VMG is always available so the frontier can
        tack on the layline instead of overstanding.

        After a mark snap (``maneuver=="mark"``) the full fan is offered so the
        boat can turn around in a channel instead of being locked to the approach.
        """
        headings: set[float] = set()
        brg = bearing(node.pos, target)
        up_twa = self.boat.target_upwind_twa(tws)
        dn_twa = self.boat.target_downwind_twa(tws)
        port_up = norm360(twd + up_twa)
        stbd_up = norm360(twd - up_twa)
        port_dn = norm360(twd + dn_twa)
        stbd_dn = norm360(twd - dn_twa)

        twa_brg = twa_from(brg, twd)
        # Only steer straight at the mark when that heading is in the efficient band.
        if up_twa <= twa_brg <= dn_twa:
            headings.add(brg)
        elif twa_brg < up_twa:
            headings.add(
                port_up
                if abs(signed_diff(port_up, brg)) <= abs(signed_diff(stbd_up, brg))
                else stbd_up
            )
        else:
            headings.add(
                port_dn
                if abs(signed_diff(port_dn, brg)) <= abs(signed_diff(stbd_dn, brg))
                else stbd_dn
            )

        # Free fan at start-of-leg / leaving a mark / explicit override (channel retry).
        free_fan = (
            node.heading is None
            or node.maneuver == "mark"
            or fan_half_override is not None and fan_half_override >= 180.0
        )
        if free_fan:
            headings.update(self._fan_headings)
            headings.update((port_up, stbd_up, port_dn, stbd_dn))
            return list(headings)

        headings.add(node.heading)

        fan_half = 45.0 if fan_half_override is None else fan_half_override
        if fan_half_override is None:
            probe = destination(
                node.pos, brg if up_twa <= twa_brg <= dn_twa else node.heading, 0.15
            )
            if self.geo.blocked_segment(node.pos, probe) or self.geo.blocked_point(probe):
                fan_half = 90.0

        for h in self._fan_headings:
            if abs(signed_diff(h, node.heading)) <= fan_half:
                headings.add(h)

        # Same-tack VMG always; opposite only when overstanding / near / can't point —
        # offering both every step with a light prune cost reintroduces short-tacking.
        same_up = (
            port_up
            if abs(signed_diff(port_up, node.heading)) <= abs(signed_diff(stbd_up, node.heading))
            else stbd_up
        )
        opp_up = stbd_up if same_up == port_up else port_up
        same_dn = (
            port_dn
            if abs(signed_diff(port_dn, node.heading)) <= abs(signed_diff(stbd_dn, node.heading))
            else stbd_dn
        )
        opp_dn = stbd_dn if same_dn == port_dn else port_dn
        headings.add(same_up)
        headings.add(same_dn)
        rem = distance_nm(node.pos, target)
        course_err = abs(signed_diff(brg, node.heading))
        # Opposite tack when clearly overstanding / can't point — not for the whole
        # final 1.5 nm (that reintroduces short-tacking into the mark).
        if (
            course_err > 55.0
            or rem < 0.6
            or twa_from(node.heading, twd) < up_twa - 5.0
        ):
            headings.add(opp_up)
            headings.add(opp_dn)
        return list(headings)

    def _score(self, node: Node, target: tuple[float, float], origin: tuple[float, float]) -> float:
        return _prune_score(
            node,
            target,
            origin,
            self.p.maneuver_prune_nm,
            self.p.max_consecutive_maneuvers,
            self.p.maneuver_overflow_nm,
            self.p.heading_continuity_nm,
        )

    def _prune(
        self,
        nodes: list[Node],
        target: tuple[float, float],
        origin: tuple[float, float],
    ) -> list[Node]:
        """Isochrone prune: bearing buckets from origin, ranked by prune score.

        Always re-inserts the globally best remaining-to-target node so a leg
        cannot die from over-aggressive sector/cell bucketing alone.
        """
        if not nodes:
            return nodes
        best_dmg = min(nodes, key=lambda n: _remaining_nm(n, target))
        by_sector: dict[int, Node] = {}
        for n in nodes:
            sec = int(bearing(origin, n.pos) // self.p.sector_width_deg)
            cur = by_sector.get(sec)
            if cur is None or self._score(n, target, origin) < self._score(cur, target, origin):
                by_sector[sec] = n
        kept = list(by_sector.values())
        by_cell: dict[tuple[int, int], Node] = {}
        for n in kept:
            cell = (
                int(n.lat / self.p.cluster_cell_deg),
                int(n.lon / self.p.cluster_cell_deg),
            )
            cur = by_cell.get(cell)
            if cur is None or self._score(n, target, origin) < self._score(cur, target, origin):
                by_cell[cell] = n
        kept = list(by_cell.values())
        if best_dmg not in kept:
            kept.append(best_dmg)
        if len(kept) > self.p.max_frontier:
            kept.sort(key=lambda n: self._score(n, target, origin))
            # Keep best-DMG even if it ranks poorly on prune score.
            trimmed = kept[: self.p.max_frontier]
            if best_dmg not in trimmed:
                trimmed[-1] = best_dmg
            kept = trimmed
        return kept

    def _expand_frontier(
        self,
        frontier: list[Node],
        tgt: tuple[float, float],
        step_s: int,
        *,
        fan_half_override: float | None,
    ) -> tuple[list[Node], int, int]:
        """Expand every frontier node for one time step.

        Returns (children, n_heading_attempts, n_geo_blocked).
        """
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

            for heading in self._candidate_headings(
                node, tws, twd, tgt, fan_half_override=fan_half_override
            ):
                n_tried += 1
                twa = twa_from(heading, twd)
                bsp = self.boat.bsp(tws, twa)
                if bsp <= 0:
                    continue
                if bsp_floor > 0 and bsp < bsp_floor:
                    continue
                maneuver = _detect_maneuver(node, heading, twd, twa)
                pen = 0.0
                if maneuver == "tack":
                    pen = self.boat.config.tack_penalty_s
                elif maneuver == "gybe":
                    pen = self.boat.config.gybe_penalty_s

                ctw = heading + self.boat.leeway_offset(tws, bsp, twd, heading)
                vx, vy = vector(norm360(ctw), bsp)
                # Soft hazard disabled: depth is hard-cutoff only via geo.blocked_*.
                # Optional HazardGrid (explicit ctor arg) still applies for tests.
                haz = 0.0
                if self.hazard is not None:
                    haz = min(self.hazard.cost_at((node.lat, node.lon)), step_s * 0.2)
                eff_dt = max(0.0, step_s - pen - haz)
                nlat, nlon = _advance(node.lat, node.lon, vx, vy, eff_dt)

                if self.geo.blocked_segment(node.pos, (nlat, nlon)):
                    n_geo_hit += 1
                    continue
                if self.geo.blocked_point((nlat, nlon)):
                    n_geo_hit += 1
                    continue

                # Effective SOG matches displacement (hazard/tack time-loss).
                sog_eff = bsp * (eff_dt / step_s) if step_s > 0 else bsp
                _, cog = magnitude_bearing(vx, vy)
                n_man = node.n_maneuvers + (1 if maneuver in ("tack", "gybe") else 0)
                depth_m = (
                    self.geo.depth.depth((nlat, nlon)) if self.geo.depth is not None else 0.0
                )
                nxt.append(
                    Node(
                        lat=nlat,
                        lon=nlon,
                        t=node.t + timedelta(seconds=step_s),
                        parent=node,
                        heading=heading,
                        bsp=bsp,
                        tws=tws,
                        twd=twd,
                        twa=twa,
                        sog=sog_eff,
                        cog=cog,
                        maneuver=maneuver,
                        n_maneuvers=n_man,
                        depth_m=depth_m,
                        hazard_cost=haz,
                    )
                )
        return nxt, n_tried, n_geo_hit

    @staticmethod
    def _arrivals_crossing_mark(children: list[Node], target: Mark) -> list[Node]:
        """Detect mid-step entries into the mark radius along parent→child segments."""
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
            # Sample the segment for a crossing into the radius.
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
                            hazard_cost=child.hazard_cost,
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
    ) -> tuple[Node | None, bool]:
        """Expand until a node reaches the target radius. Returns (arrival, exhausted)."""
        tgt = target.position
        origin = frontier[0].pos
        step = self.p.time_step_s
        best_remaining = min(_remaining_nm(n, tgt) for n in frontier)
        initial_remaining = best_remaining
        # Scale stall limit with leg length — upwind beats often stall GC distance
        # for many steps while still making progress along the layline.
        stall_limit = max(
            self.p.no_progress_steps,
            int(initial_remaining * 4.0) + 40,
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
            rem_now = min(_remaining_nm(n, tgt) for n in frontier)
            # Finer steps near the mark so we don't jump past the layline/radius.
            step_now = step
            if rem_now < self.p.near_mark_nm:
                step_now = max(60, step // 2)

            nxt, n_tried, n_geo_hit = self._expand_frontier(
                frontier, tgt, step_now, fan_half_override=None
            )
            n_wind += len(frontier)
            n_blocked += n_geo_hit
            n_expansions += len(nxt)

            # Adaptive: if most candidates hit geo, retry with half step.
            if n_tried > 0 and n_geo_hit / n_tried > 0.5 and nxt:
                half = max(60, step_now // 2)
                if half < step_now:
                    nxt2, tried2, hit2 = self._expand_frontier(
                        frontier, tgt, half, fan_half_override=None
                    )
                    n_blocked += hit2
                    if len(nxt2) > len(nxt):
                        nxt = nxt2
                        step_now = half
                        n_expansions += len(nxt2)

            any_wind = n_tried > 0 or bool(nxt)
            if not any_wind and not nxt:
                # Distinguish no-wind from all-blocked: probe one node.
                w0 = self.wind.wind_at(frontier[0].lat, frontier[0].lon, frontier[0].t)
                if w0 is None:
                    termination = "forecast_exhausted"
                    exhausted = True
                    arrival = None
                    break

            # Channel escape: if everything was geo-blocked, retry once with half
            # step + full 180° fan before declaring unreachable.
            if not nxt:
                half = max(60, step_now // 2)
                nxt, _, hit_r = self._expand_frontier(
                    frontier, tgt, half, fan_half_override=180.0
                )
                n_blocked += hit_r
                n_expansions += len(nxt)
                step_now = half

            if not nxt:
                self._unreachable = True
                termination = "frontier_empty"
                self._leg_warnings.append(
                    f"mark '{name}' unreachable (frontier empty after {steps_taken} steps)"
                )
                arrival = None
                break

            # Segment–circle: did any expansion cross into the mark radius mid-step?
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
        if arrival is None and exhausted:
            return None, True
        if arrival is None:
            return None, False
        return arrival, False

    @staticmethod
    def _snap_to_mark(node: Node, target: Mark) -> Node:
        """Replace an in-radius arrival with a node exactly on the mark.

        Heading/COG become the approach bearing from the parent so the final
        segment of the leg points at the mark. Telemetry (bsp/tws/…) is kept
        from the discovering node.
        """
        mark_lat, mark_lon = target.position
        if node.parent is not None:
            brg = bearing(node.parent.pos, target.position)
        elif node.heading is not None:
            brg = node.heading
        else:
            brg = 0.0
        return Node(
            lat=mark_lat,
            lon=mark_lon,
            t=node.t,
            parent=node.parent,
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
            hazard_cost=node.hazard_cost,
        )

    def route(self, course: Course, model_id: str) -> ModelRoute:
        self.model_id = model_id
        depart = course.depart_utc
        start_depth = (
            self.geo.depth.depth(course.start) if self.geo.depth is not None else 0.0
        )
        start_node = Node(
            lat=course.start[0],
            lon=course.start[1],
            t=depart,
            depth_m=start_depth,
            hazard_cost=self.hazard.cost_at(course.start) if self.hazard else 0.0,
        )

        targets: list[Mark] = list(course.marks)
        targets.append(Mark(position=course.finish, radius_nm=0.2, rounding="either", name="finish"))

        frontier = [start_node]
        exhausted = False
        last_arrival: Node | None = start_node
        self._unreachable = False
        self._leg_warnings = []
        leg_total = len(targets)

        for i, target in enumerate(targets):
            arrival, exh = self._expand_leg(
                frontier, target, leg_index=i, leg_total=leg_total
            )
            if arrival is None:
                exhausted = exh
                self._unreachable = True
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
                    hazard_cost=round(node.hazard_cost, 2),
                )
            )
        return legs
