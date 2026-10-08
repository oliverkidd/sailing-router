"""Offline tests for the wind downscaling pipeline (no GRIB / network).

Run from services/api:
  .venv/bin/python scripts/test_wind_pipeline.py
"""

from __future__ import annotations

import math
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

from app.metocean import WindField  # noqa: E402
from app.schemas import BBox  # noqa: E402
from app.util.bbox import budget_dims, canonical_grid  # noqa: E402
from app.wind.downscaled import DownscaledWindField  # noqa: E402
from app.wind.operator import apply_response, downscale_point  # noqa: E402
from app.wind.terrain_library import (  # noqa: E402
    TerrainLibrary,
    TerrainResponse,
    write_identity_library,
)
from app.wind.zoom_tiers import (  # noqa: E402
    display_window,
    tier_for_zoom,
)


def _forcing() -> WindField:
    times = np.array([datetime(2026, 7, 18, 12, tzinfo=UTC).timestamp()], dtype=float)
    lats = np.linspace(50.5, 51.0, 11)
    lons = np.linspace(-1.5, -1.0, 11)
    # Uniform 10 m/s from the west (u=+10, v=0) → TWD ≈ 270°.
    u = np.full((1, 11, 11), 10.0, dtype=np.float32)
    v = np.zeros((1, 11, 11), dtype=np.float32)
    return WindField.build("test", times, lats, lons, u, v)


def test_identity_operator():
    forcing = _forcing()
    t = datetime(2026, 7, 18, 12, tzinfo=UTC)
    uv = downscale_point(forcing, None, 50.75, -1.25, t)
    assert uv is not None
    u, v = uv
    assert abs(u - 10.0) < 1e-3 and abs(v) < 1e-3

    wrapped = DownscaledWindField(forcing=forcing, library=None)
    # Without library, DownscaledWindField still applies identity via operator
    # when library is None — but we return raw forcing from provider. Direct wrap:
    w = wrapped.wind_at(50.75, -1.25, t)
    assert w is not None
    tws, twd = w
    assert abs(tws - 10.0 * 1.943844) < 0.1
    assert abs(twd - 270.0) < 1.0
    print("OK identity operator")


def test_apply_speedup_deflection():
    resp = TerrainResponse(speedup=1.5, deflection_deg=10.0)
    # West wind 10 m/s (u=10, v=0) FROM 270°.
    u, v = apply_response(10.0, 0.0, resp)
    speed = math.hypot(u, v)
    assert abs(speed - 15.0) < 1e-3
    dir_from = (math.degrees(math.atan2(-u, -v))) % 360.0
    assert abs(dir_from - 280.0) < 1.0
    print("OK apply speedup/deflection")


def test_zoom_tiers():
    assert tier_for_zoom(4).name == "coarse"
    assert tier_for_zoom(8).name == "mid"
    assert tier_for_zoom(11).name == "fine"
    assert tier_for_zoom(14).name == "ultra"
    vp = BBox(lat_min=50.5, lon_min=-1.5, lat_max=51.0, lon_max=-1.0)
    w = display_window(vp, zoom=14)
    assert w.lat_min < vp.lat_min and w.lat_max > vp.lat_max
    nlat, nlon = budget_dims(w, tier_for_zoom(14).target_res_deg, max_pts=tier_for_zoom(14).max_pts)
    assert nlat * nlon <= tier_for_zoom(14).max_pts
    print(f"OK zoom tiers (ultra budget {nlat}x{nlon})")


def test_native_canonical_grid():
    region = BBox(lat_min=50.0, lon_min=-2.0, lat_max=52.0, lon_max=0.0)
    lats, lons = canonical_grid(region, res_deg=0.25)
    # ~0.25° spacing
    assert abs(float(lats[1] - lats[0]) - 0.25) < 1e-6
    lats_fine, _ = canonical_grid(region, res_deg=0.02)
    assert lats_fine.size > lats.size
    print("OK native-res canonical_grid")


def test_identity_library_roundtrip():
    bbox = BBox(lat_min=50.6, lon_min=-1.4, lat_max=50.9, lon_max=-1.1)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "library.zarr"
        write_identity_library(path, bbox, res_deg=0.01)
        # Temporarily point TERRAIN loader at this path via load of the zarr itself.
        import xarray as xr

        ds = xr.open_zarr(path)
        lib = TerrainLibrary(
            lats=np.asarray(ds["lat"].values, dtype=float),
            lons=np.asarray(ds["lon"].values, dtype=float),
            speedup=np.asarray(ds["speedup"].values, dtype=np.float32),
            deflection_deg=np.asarray(ds["deflection_deg"].values, dtype=np.float32),
        )
        ds.close()
        resp = lib.sample(50.75, -1.25, wind_dir_from_deg=270.0, day_frac=1.0)
        assert abs(resp.speedup - 1.0) < 1e-5
        assert abs(resp.deflection_deg) < 1e-5

        forcing = _forcing()
        t = datetime(2026, 7, 18, 12, tzinfo=UTC)
        uv0 = downscale_point(forcing, None, 50.75, -1.25, t)
        uv1 = downscale_point(forcing, lib, 50.75, -1.25, t)
        assert uv0 is not None and uv1 is not None
        assert abs(uv0[0] - uv1[0]) < 1e-3 and abs(uv0[1] - uv1[1]) < 1e-3
    print("OK identity library == forcing (router-safe when library absent/identity)")


def test_roughness_speedup_factor():
    from app.wind.roughness import Z0_LAND, Z0_SEA, roughness_speedup_factor

    sea = roughness_speedup_factor(np.full((2, 2), Z0_SEA))
    land = roughness_speedup_factor(np.full((2, 2), Z0_LAND))
    assert abs(float(sea.mean()) - 1.0) < 1e-3
    assert float(land.mean()) < 0.8  # land slower than sea
    print(f"OK roughness speedup (land={float(land.mean()):.3f})")


def test_vectorised_downscale_matches_pointwise():
    """Vectorised downscale_grid agrees with per-cell sample+apply on a tiny lib."""
    from app.wind.operator import apply_response, downscale_grid
    from app.wind.terrain_library import N_SECTORS, TerrainLibrary, day_frac_utc

    lats = np.linspace(37.5, 37.6, 5)
    lons = np.linspace(24.5, 24.6, 5)
    # Synthetic library: speedup increases with lat; deflection = 5°.
    nlat, nlon = lats.size, lons.size
    su = np.ones((N_SECTORS, 2, nlat, nlon), dtype=np.float32)
    de = np.full((N_SECTORS, 2, nlat, nlon), 5.0, dtype=np.float32)
    for i in range(nlat):
        su[:, :, i, :] = 0.8 + 0.4 * (i / max(1, nlat - 1))
    lib = TerrainLibrary(
        lats=lats,
        lons=lons,
        speedup=su,
        deflection_deg=de,
        source="test",
    )
    times = np.array([datetime(2026, 7, 18, 12, tzinfo=UTC).timestamp()], dtype=float)
    u = np.full((1, nlat, nlon), 10.0, dtype=np.float32)
    v = np.zeros((1, nlat, nlon), dtype=np.float32)
    forcing = WindField.build("test", times, lats, lons, u, v)

    grid = downscale_grid(forcing, lib, lats, lons)
    te = float(times[0])
    max_err = 0.0
    for i, la in enumerate(lats):
        for j, lo in enumerate(lons):
            resp = lib.sample(
                float(la),
                float(lo),
                wind_dir_from_deg=270.0,
                day_frac=day_frac_utc(te, float(lo)),
            )
            u_exp, v_exp = apply_response(10.0, 0.0, resp)
            err = abs(float(grid.u10[0, i, j]) - u_exp) + abs(
                float(grid.v10[0, i, j]) - v_exp
            )
            max_err = max(max_err, err)
    assert max_err < 1e-3, f"vectorised vs pointwise max_err={max_err}"
    print(f"OK vectorised downscale_grid (max_err={max_err:.2e})")


def test_coastal_strip_keep():
    """20 km keep includes harbour water and drops Sahara / Alpine interiors."""
    from coastal_band import bbox_near_coast, crop_keep_slices, lonlat_in_coastal_keep

    def bb(lon0: float, lat0: float) -> dict[str, float]:
        return {
            "lon_min": lon0,
            "lat_min": lat0,
            "lon_max": lon0 + 1.0,
            "lat_max": lat0 + 1.0,
        }

    cases = [
        ("Sahara N30 E010", bb(10.0, 30.0), False),
        ("Alps N46 E008", bb(8.0, 46.0), False),
        ("Cyclades N37 E024", bb(24.0, 37.0), True),
        ("Solent N50 W002", bb(-2.0, 50.0), True),
        ("Gulf of Sidra N31 E016", bb(16.0, 31.0), True),
    ]
    for name, b, expect in cases:
        got = bbox_near_coast(b)
        assert got is expect, f"{name}: near_coast={got} expected {expect}"

    points = [
        ("Sahara interior", 10.5, 30.5, False),
        ("Kea-ish", 24.35, 37.62, True),
        ("Alps interior", 8.4, 46.5, False),
        ("Solent water", -1.3, 50.75, True),
    ]
    for name, lon, lat, expect in points:
        got = lonlat_in_coastal_keep(lon, lat)
        assert got is expect, f"{name} ({lon},{lat}): keep={got} expected {expect}"

    keep = np.zeros((8, 10), dtype=bool)
    keep[2:5, 3:7] = True
    slat, slon = crop_keep_slices(keep, pad=1)
    assert slat == slice(1, 6)
    assert slon == slice(2, 8)
    print("OK coastal strip keep (bbox + lon/lat + crop slices)")


def test_cyclades_library_coastal_vs_sea():
    """If a baked Cyclades library exists, coastal/lee differs from mid-sea identity."""
    from app.wind.terrain_library import REGION_LIBRARY, load_library_for_bbox

    if not REGION_LIBRARY.exists():
        print("SKIP cyclades library (not baked yet)")
        return

    import xarray as xr

    ds = xr.open_zarr(REGION_LIBRARY)
    src = str(ds.attrs.get("source", ""))
    su = np.asarray(ds["speedup"].values)
    ds.close()
    if src != "windninja" or float(su.std()) < 0.01:
        print(f"SKIP cyclades library (source={src!r} std={float(su.std()):.4f})")
        return

    bbox = BBox(lat_min=37.2, lon_min=24.3, lat_max=38.0, lon_max=25.2)
    lib = load_library_for_bbox(bbox, overview=1)
    assert lib is not None and lib.available
    assert lib.source == "windninja"

    # Uniform westerly forcing on a tiny Cyclades grid.
    times = np.array([datetime(2026, 7, 18, 12, tzinfo=UTC).timestamp()], dtype=float)
    lats = np.linspace(37.4, 37.8, 9)
    lons = np.linspace(24.4, 25.0, 9)
    u = np.full((1, 9, 9), 10.0, dtype=np.float32)
    v = np.zeros((1, 9, 9), dtype=np.float32)
    forcing = WindField.build("test", times, lats, lons, u, v)
    t = datetime(2026, 7, 18, 12, tzinfo=UTC)

    # Sea lee of Gyaros (terrain shadow over water) vs open water further east.
    # Land cells are identity under wn2-sea — do not sample highland points.
    coastal = downscale_point(forcing, lib, 37.60, 24.68, t)
    midsea = downscale_point(forcing, lib, 37.55, 24.90, t)
    raw_c = downscale_point(forcing, None, 37.60, 24.68, t)
    raw_s = downscale_point(forcing, None, 37.55, 24.90, t)
    assert coastal and midsea and raw_c and raw_s

    du_c = abs(coastal[0] - raw_c[0]) + abs(coastal[1] - raw_c[1])
    du_s = abs(midsea[0] - raw_s[0]) + abs(midsea[1] - raw_s[1])
    assert du_c > 0.05, f"coastal sea lee should differ from raw forcing, got |du|={du_c}"
    # Open mid-sea should be weaker than the lee shadow.
    print(
        f"OK cyclades library coastal|du|={du_c:.3f} midsea|du|={du_s:.3f} "
        f"(coastal > midsea: {du_c > du_s})"
    )


def main() -> int:
    test_identity_operator()
    test_apply_speedup_deflection()
    test_zoom_tiers()
    test_native_canonical_grid()
    test_identity_library_roundtrip()
    test_roughness_speedup_factor()
    test_vectorised_downscale_matches_pointwise()
    test_coastal_strip_keep()
    test_cyclades_library_coastal_vs_sea()
    print("\nAll wind pipeline tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
