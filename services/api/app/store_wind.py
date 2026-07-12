"""Zarr-backed canonical WindField store."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import xarray as xr

from .config import CACHE_DIR
from .metocean import WindField
from .schemas import BBox

FORECAST_DIR = CACHE_DIR / "forecast"
FORECAST_DIR.mkdir(parents=True, exist_ok=True)


def zarr_path(model_id: str, cycle: datetime, region: BBox) -> Path:
    stamp = cycle.strftime("%Y%m%d%H")
    return FORECAST_DIR / f"{model_id}__{region.key()}__{stamp}.zarr"


def write_field(field: WindField, cycle: datetime, region: BBox) -> Path:
    """Persist a WindField as a Zarr group (chunked by time)."""
    path = zarr_path(field.model_id, cycle, region)
    if path.exists():
        # Overwrite stale store for the same key.
        import shutil  # noqa: PLC0415

        shutil.rmtree(path)
    times = np.asarray(field.times, dtype="datetime64[s]")
    ds = xr.Dataset(
        data_vars={
            "u10": (("time", "lat", "lon"), field.u10.astype(np.float32)),
            "v10": (("time", "lat", "lon"), field.v10.astype(np.float32)),
        },
        coords={
            "time": times,
            "lat": field.lats.astype(np.float64),
            "lon": field.lons.astype(np.float64),
        },
        attrs={"model_id": field.model_id, "cycle": cycle.isoformat()},
    )
    ds.to_zarr(path, mode="w", zarr_format=2)
    return path


def read_field(path: Path | str) -> WindField:
    """Load a WindField from a Zarr group on disk (float32; close after read)."""
    path = Path(path)
    ds = xr.open_zarr(path)
    try:
        model_id = str(ds.attrs.get("model_id", path.stem.split("__")[0]))
        times = ds["time"].values
        epochs = []
        for t0 in np.atleast_1d(times):
            epochs.append(
                float(np.datetime64(t0, "s").astype("datetime64[s]").astype(np.int64))
            )
        return WindField.build(
            model_id,
            epochs,
            np.asarray(ds["lat"].values, dtype=np.float64),
            np.asarray(ds["lon"].values, dtype=np.float64),
            np.asarray(ds["u10"].values, dtype=np.float32),
            np.asarray(ds["v10"].values, dtype=np.float32),
        )
    finally:
        ds.close()
