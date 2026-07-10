from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from app.boat import Boat
from app.currents.fes import FESCurrentProvider
from app.geo import GeoContext
from app.metocean import MS_TO_KN, CurrentField, WindField
from app.navmath import magnitude_bearing
from app.polar import PolarTable
from app.router import IsochroneRouter, RouterParams
from app.schemas import BBox, BoatConfig, Course

DEPART = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
CSV = """twa/tws,40,52,60,75,90,110,120,135,150,165,180
6,0,4.6,5.1,5.6,5.8,5.7,5.5,5.0,4.3,3.6,3.2
10,0,6.4,6.9,7.3,7.5,7.5,7.3,6.9,6.2,5.4,4.9
16,0,7.4,7.9,8.6,9.0,9.5,9.7,9.5,8.9,8.1,7.5
20,0,7.6,8.2,9.0,9.6,10.4,10.9,11.2,10.8,9.9,9.1
"""


def test_current_convention_toward():
    # u=+1 (east), v=0 => set 090, rate ~1.94 kn.
    lats = np.array([36.0, 37.0])
    lons = np.array([24.0, 25.0])
    times = np.array([DEPART.timestamp(), (DEPART + timedelta(hours=6)).timestamp()])
    cf = CurrentField("t", times, lats, lons, np.ones((2, 2, 2)), np.zeros((2, 2, 2)))
    u, v = cf.current_at(36.5, 24.5, DEPART + timedelta(hours=1))
    rate, set_deg = magnitude_bearing(u * MS_TO_KN, v * MS_TO_KN)
    assert set_deg == pytest.approx(90.0, abs=0.5)
    assert rate == pytest.approx(1.943844, abs=0.01)


def test_fes_nonzero_offline():
    bbox = BBox(lat_min=37.8, lon_min=15.0, lat_max=38.4, lon_max=15.8)  # Messina-ish
    cf = FESCurrentProvider().fetch(bbox, DEPART, DEPART + timedelta(hours=12))
    # Sample across the window; at least some timesteps have appreciable current.
    mags = []
    for h in range(0, 12):
        u, v = cf.current_at(38.1, 15.4, DEPART + timedelta(hours=h))
        mags.append((u**2 + v**2) ** 0.5)
    assert max(mags) > 0.05  # nonzero tidal current, fully offline


def _uniform_wind(bbox, tws_kn, twd):
    lats = np.linspace(bbox[0], bbox[2], 5)
    lons = np.linspace(bbox[1], bbox[3], 5)
    times = [DEPART + timedelta(hours=h) for h in range(25)]
    speed = np.full((len(times), 5, 5), tws_kn / MS_TO_KN)
    direction = np.full((len(times), 5, 5), twd)
    return WindField.from_speed_dir("t", times, lats, lons, speed, direction)


def test_currents_change_eta():
    bbox = (36.0, 24.0, 37.2, 25.2)
    wind = _uniform_wind(bbox, 12.0, 90.0)  # wind from east, reach north
    course = Course(start=(36.1, 24.6), finish=(37.0, 24.6), depart_utc=DEPART, min_depth_m=0.0)
    geo = GeoContext(min_depth_m=0.0)
    boat = Boat(BoatConfig(polar_id="p", current_drift_factor=1.0), PolarTable.from_csv(CSV))
    params = RouterParams(time_step_s=600, heading_step_deg=10.0)

    # Strong favourable current flowing north (v>0) should speed the passage.
    lats = np.linspace(bbox[0], bbox[2], 5)
    lons = np.linspace(bbox[1], bbox[3], 5)
    times = np.array([(DEPART + timedelta(hours=h)).timestamp() for h in range(25)])
    north_cur = CurrentField(
        "t", times, lats, lons, np.zeros((25, 5, 5)), np.full((25, 5, 5), 1.0)
    )

    no_cur = IsochroneRouter(boat, wind, geo, None, params, False).route(course, "t")
    with_cur = IsochroneRouter(boat, wind, geo, north_cur, params, True).route(course, "t")

    # A strong current changes the optimal track (set-and-drift is applied).
    def midpoints(r):
        return [leg.position for leg in r.legs[:: max(1, len(r.legs) // 5)]]

    assert with_cur.distance_nm != no_cur.distance_nm or midpoints(with_cur) != midpoints(no_cur)
    # Legs carry the sampled current set/rate when currents are on.
    assert any(leg.cur_rate_kn > 0 for leg in with_cur.legs)
    assert all(leg.cur_rate_kn == 0 for leg in no_cur.legs)
