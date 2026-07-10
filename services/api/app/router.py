"""Isochrone router: terrain-adjusted wind + current set-and-drift, one route per model.

Run once per selected model using that model's (optionally terrain-downscaled)
WindField and the shared CurrentField. See docs/build-plan.md §7.6 for the spec.

The frontier is expanded in fixed time steps; at each step every live node fans out
over candidate headings, each producing a child node after applying the polar,
leeway, current set-and-drift, and the land/depth/exclusion masks. The frontier is
pruned three ways (bearing sector, grid cluster, hard cap) to stay bounded, then we
backtrack parent pointers from the best finish node into a list of RouteLegs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from .boat import Boat
from .geo import GeoContext, bearing, destination, distance_nm
from .metocean import MS_TO_KN, WindField
from .navmath import magnitude_bearing, norm360, signed_diff, twa_from, vector
from .schemas import Course, Mark, ModelRoute, RouteLeg

KN_PER_MS = MS_TO_KN


class CurrentSampler(Protocol):
    def current_at(self, lat: float, lon: float, t: datetime) -> tuple[float, float]:
        """Return (u, v) surface current in m/s (east, north)."""
        ...


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
    cur_set: float = 0.0
    cur_rate: float = 0.0
    maneuver: str = "none"

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


def _wind_side(heading: float, twd: float) -> float:
    """+1 if wind is on one side of the heading, -1 the other, 0 head/dead on."""
    d = signed_diff(twd, heading)
    if abs(d) < 1e-6 or abs(abs(d) - 180.0) < 1e-6:
        return 0.0
    return 1.0 if d > 0 else -1.0


def _detect_maneuver(parent: Node, heading: float, twd: float, twa: float) -> str:
    if parent.heading is None:
        return "none"
    if _wind_side(parent.heading, parent.twd) == _wind_side(heading, twd):
        return "none"
    # Wind crossed to the other side: upwind => tack, downwind => gybe.
    return "tack" if twa < 90.0 else "gybe"


def _advance(lat: float, lon: float, gx: float, gy: float, dt_s: float) -> tuple[float, float]:
    """Integrate ground velocity (knots east/north) over dt_s seconds."""
    speed, brg = magnitude_bearing(gx, gy)
    dist = speed * (dt_s / 3600.0)
    if dist <= 0:
        return lat, lon
    return destination((lat, lon), brg, dist)


class IsochroneRouter:
    def __init__(
        self,
        boat: Boat,
        wind: WindField,
        geo: GeoContext,
        current: CurrentSampler | None,
        params: RouterParams,
        use_currents: bool = True,
    ):
        self.boat = boat
        self.wind = wind
        self.geo = geo
        self.current = current if use_currents else None
        self.p = params
        self._headings = [
            h * params.heading_step_deg
            for h in range(int(round(360.0 / params.heading_step_deg)))
        ]

    # --- pruning ------------------------------------------------------------

    def _prune(self, nodes: list[Node], target: tuple[float, float]) -> list[Node]:
        if not nodes:
            return nodes
        # 1) best (closest to target) per bearing sector.
        by_sector: dict[int, Node] = {}
        for n in nodes:
            sec = int(bearing(target, n.pos) // self.p.sector_width_deg)
            cur = by_sector.get(sec)
            if cur is None or distance_nm(n.pos, target) < distance_nm(cur.pos, target):
                by_sector[sec] = n
        kept = list(by_sector.values())
        # 2) best per grid cell.
        by_cell: dict[tuple[int, int], Node] = {}
        for n in kept:
            cell = (
                int(n.lat / self.p.cluster_cell_deg),
                int(n.lon / self.p.cluster_cell_deg),
            )
            cur = by_cell.get(cell)
            if cur is None or distance_nm(n.pos, target) < distance_nm(cur.pos, target):
                by_cell[cell] = n
        kept = list(by_cell.values())
        # 3) hard cap: keep the closest max_frontier to target.
        if len(kept) > self.p.max_frontier:
            kept.sort(key=lambda n: distance_nm(n.pos, target))
            kept = kept[: self.p.max_frontier]
        return kept

    # --- expansion ----------------------------------------------------------

    def _expand_leg(
        self, frontier: list[Node], target: Mark
    ) -> tuple[Node | None, bool]:
        """Expand until a node reaches the target radius. Returns (arrival, exhausted)."""
        tgt = target.position
        step = self.p.time_step_s
        for _ in range(self.p.max_steps_per_leg):
            nxt: list[Node] = []
            any_wind = False
            for node in frontier:
                w = self.wind.wind_at(node.lat, node.lon, node.t)
                if w is None:
                    continue
                any_wind = True
                tws, twd = w
                if self.current is not None:
                    cu, cv = self.current.current_at(node.lat, node.lon, node.t)
                else:
                    cu, cv = 0.0, 0.0
                cur_rate, cur_set = magnitude_bearing(cu * KN_PER_MS, cv * KN_PER_MS)
                cdf = self.boat.config.current_drift_factor

                for heading in self._headings:
                    twa = twa_from(heading, twd)
                    bsp = self.boat.bsp(tws, twa)
                    if bsp <= 0:
                        continue
                    maneuver = _detect_maneuver(node, heading, twd, twa)
                    pen = 0.0
                    if maneuver == "tack":
                        pen = self.boat.config.tack_penalty_s
                    elif maneuver == "gybe":
                        pen = self.boat.config.gybe_penalty_s

                    ctw = heading + self.boat.leeway_offset(tws, bsp, twd, heading)
                    vx, vy = vector(norm360(ctw), bsp)
                    gx = vx + cu * KN_PER_MS * cdf
                    gy = vy + cv * KN_PER_MS * cdf
                    eff_dt = max(0.0, step - pen)
                    nlat, nlon = _advance(node.lat, node.lon, gx, gy, eff_dt)

                    if self.geo.blocked_segment(node.pos, (nlat, nlon)):
                        continue
                    if self.geo.blocked_point((nlat, nlon)):
                        continue

                    sog, cog = magnitude_bearing(gx, gy)
                    nxt.append(
                        Node(
                            lat=nlat,
                            lon=nlon,
                            t=node.t + timedelta(seconds=step),
                            parent=node,
                            heading=heading,
                            bsp=bsp,
                            tws=tws,
                            twd=twd,
                            twa=twa,
                            sog=sog,
                            cog=cog,
                            cur_set=cur_set,
                            cur_rate=cur_rate,
                            maneuver=maneuver,
                        )
                    )

            if not any_wind:
                return None, True  # forecast exhausted for all nodes

            frontier = self._prune(nxt, tgt)
            if not frontier:
                return None, False

            arrivals = [n for n in frontier if distance_nm(n.pos, tgt) <= target.radius_nm]
            if arrivals:
                return min(arrivals, key=lambda n: n.t), False

        return None, False  # ran out of steps

    # --- driver -------------------------------------------------------------

    def route(self, course: Course, model_id: str) -> ModelRoute:
        depart = course.depart_utc
        start_node = Node(lat=course.start[0], lon=course.start[1], t=depart)

        # Course legs: start -> each mark -> finish.
        targets: list[Mark] = list(course.marks)
        targets.append(Mark(position=course.finish, radius_nm=0.2, rounding="either", name="finish"))

        frontier = [start_node]
        exhausted = False
        last_arrival: Node | None = start_node

        for target in targets:
            arrival, exh = self._expand_leg(frontier, target)
            if arrival is None:
                exhausted = exh
                break
            last_arrival = arrival
            frontier = [arrival]  # restart expansion from the rounded mark

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
                    cur_set_deg=round(node.cur_set, 1),
                    cur_rate_kn=round(node.cur_rate, 2),
                    maneuver=node.maneuver if node.maneuver in ("none", "tack", "gybe", "mark") else "none",  # type: ignore[arg-type]
                )
            )
        return legs
