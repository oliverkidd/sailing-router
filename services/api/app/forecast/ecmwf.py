"""ECMWF open-data GRIB sources — IFS 0.25 and AIFS 0.25 (global)."""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

import xarray as xr

from ..schemas import BBox
from .grib_source import (
    GribSource,
    SourceSpec,
    dataset_to_windfield,
    register,
)

logger = logging.getLogger(__name__)

# Quiet ecmwf-opendata / tqdm progress spam in API logs.
os.environ.setdefault("TQDM_DISABLE", "1")

# IFS: 3-hourly to 144h, then 6-hourly to 240h.
_IFS_STEPS = tuple(list(range(0, 145, 3)) + list(range(150, 241, 6)))
# AIFS: 6-hourly to 360h.
_AIFS_STEPS = tuple(range(0, 361, 6))


def _open_grib(path: str) -> xr.Dataset:
    try:
        import cfgrib  # noqa: PLC0415

        datasets = cfgrib.open_datasets(path, indexpath="")
    except Exception:  # noqa: BLE001
        datasets = [
            xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
        ]
    if not datasets:
        raise RuntimeError("ECMWF: empty GRIB datasets")
    if len(datasets) == 1:
        return datasets[0]
    return xr.merge(datasets, compat="override")


class EcmwfSource(GribSource):
    model: str = "ifs"

    def __init__(self, spec: SourceSpec):
        self.spec = spec

    def fetch(self, bbox: BBox, cycle: datetime, steps: list[int]):
        from ecmwf.opendata import Client  # noqa: PLC0415

        steps = list(steps) or [0]
        logger.info(
            "ECMWF %s fetch cycle=%s steps=%s bbox=(%.2f,%.2f)–(%.2f,%.2f)",
            self.spec.model_id,
            cycle.isoformat(),
            steps,
            bbox.lat_min,
            bbox.lon_min,
            bbox.lat_max,
            bbox.lon_max,
        )

        with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as f:
            target = f.name
        try:
            client = Client(source="ecmwf")
            kwargs: dict = {
                "date": cycle.strftime("%Y-%m-%d"),
                "time": cycle.hour,
                "type": "fc",
                "param": ["10u", "10v"],
                "step": steps,
                "model": self.model,
                "target": target,
            }
            if self.model == "ifs":
                kwargs["stream"] = "oper"
            client.retrieve(**kwargs)
            ds = _open_grib(target)
            return dataset_to_windfield(self.spec.model_id, ds, bbox)
        finally:
            Path(target).unlink(missing_ok=True)


class EcmwfIfsSource(EcmwfSource):
    model = "ifs"


class EcmwfAifsSource(EcmwfSource):
    model = "aifs-single"


register(
    EcmwfIfsSource(
        SourceSpec(
            model_id="ecmwf",
            connector="ecmwf-ifs",
            resolution_deg=0.25,
            cycle_hours=(0, 6, 12, 18),
            step_hours=_IFS_STEPS,
            publish_delay_h=7.0,
        )
    )
)

register(
    EcmwfAifsSource(
        SourceSpec(
            model_id="aifs",
            connector="ecmwf-aifs",
            resolution_deg=0.25,
            cycle_hours=(0, 6, 12, 18),
            step_hours=_AIFS_STEPS,
            publish_delay_h=7.0,
        )
    )
)
