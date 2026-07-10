"""Property + performance checks for the router (GATE 2)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import numpy as np

from app.boat import Boat
from app.config import DATA_DIR
from app.geo import GeoContext, LandMask, distance_nm
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


def uniform_wind(bbox, tws_kn, twd, hours=48):
    lats = np.linspace(bbox[0], bbox[2], 6)
    lons = np.linspace(bbox[1], bbox[3], 6)
    times = [DEPART + timedelta(hours=h) for h in range(hours + 1)]
    speed = np.full((len(times), 6, 6), tws_kn / MS_TO_KN)
    direction = np.full((len(times), 6, 6), twd)
    return WindField.from_speed_dir("t", times, lats, lons, speed, direction)


def _boat():
    return Boat(BoatConfig(polar_id="p"), PolarTable.from_csv(CSV))


def test_no_leg_violates_constraints():
    land = LandMask.from_geojson(DATA_DIR / "masks" / "land_aegean.geojson")
    bbox = (36.3, 23.9, 37.0, 25.1)
    wind = uniform_wind(bbox, 14.0, 270.0)
    exclusion = [(36.55, 24.7), (36.55, 24.85), (36.68, 24.85), (36.68, 24.7)]
    course = Course(
        start=(36.72, 24.0),
        finish=(36.72, 25.0),
        exclusions=[exclusion],
        depart_utc=DEPART,
        min_depth_m=0.0,
    )
    geo = GeoContext.build(land=land, depth=None, exclusion_rings=[exclusion], min_depth_m=0.0)
    params = RouterParams(time_step_s=600, heading_step_deg=10.0)
    route = IsochroneRouter(_boat(), wind, geo, None, params, False).route(course, "t")
    assert route.legs
    for i in range(1, len(route.legs)):
        a = route.legs[i - 1].position
        b = route.legs[i].position
        assert not land.on_land(b), "leg on land"
        assert not land.segment_crosses_land(a, b), "leg crosses land"
        assert not geo.in_exclusion(b), "leg inside exclusion"


def test_performance_frontier_bounded_and_fast():
    bbox = (36.0, 24.0, 38.5, 25.0)
    wind = uniform_wind(bbox, 16.0, 200.0, hours=72)
    course = Course(start=(36.1, 24.5), finish=(38.4, 24.5), depart_utc=DEPART, min_depth_m=0.0)
    geo = GeoContext(min_depth_m=0.0)
    params = RouterParams(time_step_s=600, heading_step_deg=10.0, max_frontier=2000)
    t0 = time.perf_counter()
    route = IsochroneRouter(_boat(), wind, geo, None, params, False).route(course, "t")
    dt = time.perf_counter() - t0
    assert route.legs
    # ~140 nm course should route quickly; generous CI budget.
    assert dt < 10.0, f"router too slow: {dt:.1f}s"
    assert distance_nm(course.start, course.finish) > 100
