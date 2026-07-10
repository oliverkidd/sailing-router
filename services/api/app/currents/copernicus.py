"""Copernicus Marine current connector (default online source).

Uses the `copernicusmarine` client (free account/token). MED ->
MEDSEA_ANALYSISFORECAST_PHY_006_013 (includes tides). UK/N-Atlantic -> global SMOC
GLOBAL_ANALYSISFORECAST_PHY_001_024 (circulation + FES tidal + Stokes). When the
client/token isn't available we raise so the caller can fall back to FES (offline).
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from ..metocean import CurrentField
from ..schemas import BBox
from .base import CurrentProvider

_PRODUCTS = {
    "copernicus-med": {
        "dataset_id": "cmems_mod_med_phy-cur_anfc_4.2km_PT1H-m",
        "note": "MEDSEA_ANALYSISFORECAST_PHY_006_013 (includes tides)",
    },
    "copernicus-smoc": {
        "dataset_id": "cmems_mod_glo_phy_anfc_merged-uv_PT1H-i",
        "note": "GLOBAL SMOC 001_024 (circulation + FES tidal + Stokes)",
    },
}


class CopernicusCurrentProvider(CurrentProvider):
    def __init__(self, product: str = "copernicus-smoc"):
        if product not in _PRODUCTS:
            raise ValueError(f"Unknown Copernicus product: {product}")
        self._product = product

    def fetch(self, bbox: BBox, start: datetime, end: datetime) -> CurrentField:
        try:
            import copernicusmarine  # noqa: PLC0415
            import xarray as xr  # noqa: PLC0415, F401
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "copernicusmarine/xarray not installed; use FES offline fallback"
            ) from exc

        spec = _PRODUCTS[self._product]
        ds = copernicusmarine.open_dataset(
            dataset_id=spec["dataset_id"],
            minimum_longitude=bbox.lon_min,
            maximum_longitude=bbox.lon_max,
            minimum_latitude=bbox.lat_min,
            maximum_latitude=bbox.lat_max,
            start_datetime=start,
            end_datetime=end,
            variables=["uo", "vo"],
        )
        # Surface layer, canonical (time, lat, lon).
        uo = ds["uo"].isel(depth=0) if "depth" in ds["uo"].dims else ds["uo"]
        vo = ds["vo"].isel(depth=0) if "depth" in ds["vo"].dims else ds["vo"]
        times = np.array([np.datetime64(t).astype("datetime64[s]").astype("int64") for t in ds["time"].values], dtype=float)
        return CurrentField(
            source=self._product,
            times=times,
            lats=np.asarray(ds["latitude"].values, dtype=float),
            lons=np.asarray(ds["longitude"].values, dtype=float),
            u=np.nan_to_num(np.asarray(uo.values, dtype=float)),
            v=np.nan_to_num(np.asarray(vo.values, dtype=float)),
        )
