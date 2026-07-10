from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from app.boat import Boat
from app.geo import GeoContext, LandMask
from app.metocean import MS_TO_KN, WindField
from app.polar import PolarTable
from app.router import IsochroneRouter, RouterParams
from app.schemas import BoatConfig, Course

CSV = """twa/tws,40,52,60,75,90,110,120,135,150,165,180
6,0,4.6,5.1,5.6,5.8,5.7,5.5,5.0,4.3,3.6,3.2
10,0,6.4,6.9,7.3,7.5,7.5,7.3,6.9,6.2,5.4,4.9
16,0,7.4,7.9,8.6,9.0,9.5,9.7,9.5,8.9,8.1,7.5
20,0,7.6,8.2,9.0,9.6,10.4,10.9,11.2,10.8,9.9,9.1
"""

DEPART = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)


def uniform_wind(bbox, tws_kn: float, twd_from: float, hours: int = 24) -> WindField:
    lats = np.linspace(bbox[0], bbox[2], 5)
    lons = np.linspace(bbox[1], bbox[3], 5)
    times = [DEPART + timedelta(hours=h) for h in range(hours + 1)]
    speed = np.full((len(times), lats.size, lons.size), tws_kn / MS_TO_KN)
    direction = np.full((len(times), lats.size, lons.size), twd_from)
    return WindField.from_speed_dir("test", times, lats, lons, speed, direction)


@pytest.fixture()
def boat() -> Boat:
    return Boat(BoatConfig(polar_id="p", tack_penalty_s=30, gybe_penalty_s=20), PolarTable.from_csv(CSV))


def _params() -> RouterParams:
    return RouterParams(time_step_s=600, heading_step_deg=10.0, max_frontier=1500)


def test_upwind_beat_requires_tacks(boat):
    # Wind from the north (0°); course due north => dead upwind, must tack.
    bbox = (36.0, 24.0, 37.2, 25.2)
    wind = uniform_wind(bbox, 12.0, twd_from=0.0)
    course = Course(start=(36.1, 24.6), finish=(37.0, 24.6), depart_utc=DEPART, min_depth_m=0.0)
    geo = GeoContext(min_depth_m=0.0)
    r = IsochroneRouter(boat, wind, geo, current=None, params=_params(), use_currents=False)
    route = r.route(course, "test")
    assert route.legs, "should produce a route"
    # A beat to windward cannot be sailed dead downwind-line; expect tacks.
    assert route.n_tacks >= 1
    # No leg should sail dead into the wind (twa ~0) at positive speed.
    for leg in route.legs[1:]:
        assert leg.twa >= 30.0


def test_reach_is_faster_than_beat(boat):
    bbox = (36.0, 24.0, 37.2, 25.2)
    course = Course(start=(36.1, 24.6), finish=(37.0, 24.6), depart_utc=DEPART, min_depth_m=0.0)
    geo = GeoContext(min_depth_m=0.0)
    beat = IsochroneRouter(boat, uniform_wind(bbox, 12, 0.0), geo, None, _params(), False).route(
        course, "beat"
    )
    reach = IsochroneRouter(boat, uniform_wind(bbox, 12, 90.0), geo, None, _params(), False).route(
        course, "reach"
    )
    # Beam reach (wind from east) to a northward finish is faster than a dead beat.
    assert reach.elapsed_s < beat.elapsed_s


def test_route_goes_around_island(boat):
    # Island sits between start (west) and finish (east); route must divert.
    land = LandMask.from_geojson(__import__("app.config", fromlist=["DATA_DIR"]).DATA_DIR / "masks" / "land_aegean.geojson")
    bbox = (36.4, 24.0, 37.0, 25.0)
    wind = uniform_wind(bbox, 14.0, twd_from=270.0)  # wind from west -> run east
    course = Course(start=(36.72, 24.05), finish=(36.72, 24.95), depart_utc=DEPART, min_depth_m=0.0)
    geo = GeoContext(land=land, min_depth_m=0.0)
    route = IsochroneRouter(boat, wind, geo, None, _params(), False).route(course, "test")
    assert route.legs
    # No leg may sit on the island or cross it.
    for i in range(1, len(route.legs)):
        a = route.legs[i - 1].position
        b = route.legs[i].position
        assert not land.on_land(b)
        assert not land.segment_crosses_land(a, b)
    # The diversion means max latitude deviates from the straight 36.72 line.
    lat_spread = max(leg.position[0] for leg in route.legs) - min(
        leg.position[0] for leg in route.legs
    )
    assert lat_spread > 0.03


def test_frontier_bounded(boat):
    bbox = (36.0, 24.0, 38.0, 26.0)
    wind = uniform_wind(bbox, 12.0, twd_from=0.0)
    course = Course(start=(36.1, 25.0), finish=(37.9, 25.0), depart_utc=DEPART, min_depth_m=0.0)
    params = RouterParams(time_step_s=600, heading_step_deg=10.0, max_frontier=800)
    geo = GeoContext(min_depth_m=0.0)
    r = IsochroneRouter(boat, wind, geo, None, params, False)
    # Instrument the prune to assert the cap holds.
    sizes: list[int] = []
    orig = r._prune

    def spy(nodes, target):
        out = orig(nodes, target)
        sizes.append(len(out))
        return out

    r._prune = spy  # type: ignore[method-assign]
    r.route(course, "test")
    assert sizes and max(sizes) <= 800
