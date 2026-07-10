from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from app.boat import Boat
from app.config import DATA_DIR
from app.geo import GeoContext, LandMask
from app.metocean import MS_TO_KN, WindField
from app.polar import PolarTable
from app.router import IsochroneRouter, RouterParams
from app.schemas import BoatConfig, Course
from app.terrain import TerrainProvider
from app.terrain_wind import get_terrain_wind
from app.terrain_wind.heuristic import HeuristicTerrainWind

LAND = DATA_DIR / "masks" / "land_aegean.geojson"
DEPART = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
CSV = """twa/tws,40,52,60,75,90,110,120,135,150,165,180
6,0,4.6,5.1,5.6,5.8,5.7,5.5,5.0,4.3,3.6,3.2
10,0,6.4,6.9,7.3,7.5,7.5,7.3,6.9,6.2,5.4,4.9
16,0,7.4,7.9,8.6,9.0,9.5,9.7,9.5,8.9,8.1,7.5
20,0,7.6,8.2,9.0,9.6,10.4,10.9,11.2,10.8,9.9,9.1
"""


def _wind(bbox, tws_kn, twd, hours=24):
    lats = np.linspace(bbox[0], bbox[2], 6)
    lons = np.linspace(bbox[1], bbox[3], 6)
    times = [DEPART + timedelta(hours=h) for h in range(hours + 1)]
    speed = np.full((len(times), 6, 6), tws_kn / MS_TO_KN)
    direction = np.full((len(times), 6, 6), twd)
    return WindField.from_speed_dir("m", times, lats, lons, speed, direction)


def test_lee_reduction_behind_island():
    # Uniform wind from the west (270). Points directly downwind (east) of the
    # island should show reduced speed vs the raw field; upwind ~unchanged.
    bbox = (36.4, 24.0, 37.0, 25.0)
    raw = _wind(bbox, 15.0, 270.0)
    land = LandMask.from_geojson(LAND)
    terrain = TerrainProvider(dem_path=None)  # no DEM -> default peak height
    downscaled = HeuristicTerrainWind().apply(raw, land, terrain, refine_n=30)

    t = DEPART + timedelta(hours=1)
    # Island centroid ~ (36.72, 24.45). Lee point is just east of it.
    lee = downscaled.wind_at(36.72, 24.7, t)[0]
    open_raw = raw.wind_at(36.72, 24.7, t)[0]
    assert lee < open_raw * 0.95, f"expected lee reduction, got {lee} vs {open_raw}"

    # A point far south (open water) is essentially unchanged.
    far = downscaled.wind_at(36.45, 24.7, t)[0]
    assert abs(far - open_raw) < open_raw * 0.15


def test_heuristic_fallback_when_no_binary():
    # windninja engine with no binary present must fall back to a plausible field.
    engine = get_terrain_wind("windninja")
    raw = _wind((36.4, 24.0, 37.0, 25.0), 15.0, 270.0)
    land = LandMask.from_geojson(LAND)
    out = engine.apply(raw, land, TerrainProvider(dem_path=None))
    assert out.u10.shape[1] >= 6  # refined grid produced


def test_router_differs_with_terrain_on():
    bbox = (36.4, 23.9, 37.0, 25.1)
    raw = _wind(bbox, 15.0, 270.0)
    land = LandMask.from_geojson(LAND)
    terrain = TerrainProvider(dem_path=None)
    downscaled = HeuristicTerrainWind().apply(raw, land, terrain, refine_n=30)

    course = Course(start=(36.72, 24.0), finish=(36.72, 24.95), depart_utc=DEPART, min_depth_m=0.0)
    geo = GeoContext(land=land, min_depth_m=0.0)
    boat = Boat(BoatConfig(polar_id="p"), PolarTable.from_csv(CSV))
    params = RouterParams(time_step_s=600, heading_step_deg=10.0)

    r_raw = IsochroneRouter(boat, raw, geo, None, params, False).route(course, "raw")
    r_terr = IsochroneRouter(boat, downscaled, geo, None, params, False).route(course, "terr")
    track_raw = [leg.position for leg in r_raw.legs]
    track_terr = [leg.position for leg in r_terr.legs]
    assert track_raw != track_terr or r_raw.distance_nm != r_terr.distance_nm
