from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from app import cache, store
from app.geo import course_bbox
from app.metocean import MS_TO_KN, WindField
from app.schemas import Course

DEPART = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)


def _seed(model_id, course, twd):
    bbox = course_bbox(course)
    lats = np.linspace(bbox.lat_min, bbox.lat_max, 5)
    lons = np.linspace(bbox.lon_min, bbox.lon_max, 5)
    times = [DEPART + timedelta(hours=h) for h in range(25)]
    speed = np.full((len(times), 5, 5), 12.0 / MS_TO_KN)
    direction = np.full((len(times), 5, 5), twd)
    wf = WindField.from_speed_dir(model_id, times, lats, lons, speed, direction)
    cache.save_wind(cache.wind_key(model_id, bbox, None), wf)


def test_two_models_two_distinct_routes(client):
    polar_id = store.ensure_sample()
    course = Course(start=(36.1, 24.6), finish=(37.0, 24.6), depart_utc=DEPART, min_depth_m=0.0)
    _seed("ecmwf", course, twd=20.0)   # wind from NNE
    _seed("icon", course, twd=340.0)   # wind from NNW -> different beat

    body = {
        "course": {
            "start": [36.1, 24.6],
            "finish": [37.0, 24.6],
            "marks": [],
            "exclusions": [],
            "depart_utc": DEPART.isoformat(),
            "min_depth_m": 0.0,
        },
        "boat": {"polar_id": polar_id},
        "model_ids": ["ecmwf", "icon"],
        "use_terrain_wind": False,
        "use_currents": False,
        "time_step_s": 600,
        "heading_step_deg": 10.0,
    }
    r = client.post("/route", json=body)
    assert r.status_code == 200, r.text
    routes = r.json()["routes"]
    # One route per model, no aggregation.
    assert [rt["model_id"] for rt in routes] == ["ecmwf", "icon"]
    # Different wind -> different tracks.
    t0 = [leg["position"] for leg in routes[0]["legs"]]
    t1 = [leg["position"] for leg in routes[1]["legs"]]
    assert t0 != t1
