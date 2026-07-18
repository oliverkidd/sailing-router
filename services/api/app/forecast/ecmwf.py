"""ECMWF open-data GRIB sources — IFS 0.25 and AIFS 0.25 (global)."""

from __future__ import annotations

import logging
import os
import time
import tempfile
from datetime import datetime
from pathlib import Path

import xarray as xr

from ..metocean import WindField
from ..schemas import BBox
from .grib_source import (
    ForecastRateLimitError,
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

# multiurl defaults are 500 × 120s — cap so prepare cannot hang for hours.
_MULTIURL_MAX_RETRIES = 3
_MULTIURL_RETRY_AFTER_S = 5
# Small step batches keep each CDN burst closer to scrubber-sized fetches.
_STEP_BATCH = 5


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


def _is_rate_limit(exc: BaseException) -> bool:
    text = str(exc).lower()
    if "429" in text or "too many requests" in text:
        return True
    status = getattr(exc, "response", None)
    code = getattr(status, "status_code", None) if status is not None else None
    if code == 429:
        return True
    return False


def _download_with_tight_retries(url, target, **kwargs):
    """Inject short multiurl retry budget into ecmwf-opendata's download path."""
    import ecmwf.opendata.client as odc  # noqa: PLC0415

    kwargs["maximum_retries"] = _MULTIURL_MAX_RETRIES
    kwargs["retry_after"] = _MULTIURL_RETRY_AFTER_S
    return odc._masterwind_orig_download(url, target, **kwargs)


def _ensure_download_patch() -> None:
    """Patch once: Client._download does not forward retry kwargs to multiurl."""
    import ecmwf.opendata.client as odc  # noqa: PLC0415

    if getattr(odc, "_masterwind_download_patched", False):
        return
    odc._masterwind_orig_download = odc.download
    odc.download = _download_with_tight_retries
    odc._masterwind_download_patched = True


class EcmwfSource(GribSource):
    model: str = "ifs"

    def __init__(self, spec: SourceSpec):
        self.spec = spec

    def _retrieve_batch(self, cycle: datetime, steps: list[int], target: str) -> None:
        from ecmwf.opendata import Client  # noqa: PLC0415

        _ensure_download_patch()
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
        t0 = time.perf_counter()
        logger.info(
            "ECMWF %s retrieve batch cycle=%s steps=%s",
            self.spec.model_id,
            cycle.isoformat(),
            steps,
        )
        try:
            client.retrieve(**kwargs)
            logger.info(
                "ECMWF %s batch OK %.0fms steps=%s",
                self.spec.model_id,
                (time.perf_counter() - t0) * 1000,
                steps,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if _is_rate_limit(exc):
                logger.warning(
                    "ECMWF %s rate limited after %.0fms steps=%s: %s",
                    self.spec.model_id,
                    elapsed_ms,
                    steps,
                    exc,
                )
                raise ForecastRateLimitError(self.spec.model_id, str(exc)) from exc
            logger.error(
                "ECMWF %s batch FAILED after %.0fms steps=%s: %s",
                self.spec.model_id,
                elapsed_ms,
                steps,
                exc,
            )
            raise

    def fetch(self, bbox: BBox, cycle: datetime, steps: list[int]):
        steps = list(steps) or [0]
        logger.info(
            "ECMWF %s fetch cycle=%s steps=%s (%d batches) bbox=(%.2f,%.2f)–(%.2f,%.2f)",
            self.spec.model_id,
            cycle.isoformat(),
            steps,
            (len(steps) + _STEP_BATCH - 1) // _STEP_BATCH,
            bbox.lat_min,
            bbox.lon_min,
            bbox.lat_max,
            bbox.lon_max,
        )

        fields: list[WindField] = []
        for i in range(0, len(steps), _STEP_BATCH):
            batch = steps[i : i + _STEP_BATCH]
            with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as f:
                target = f.name
            try:
                self._retrieve_batch(cycle, batch, target)
                ds = _open_grib(target)
                fields.append(dataset_to_windfield(self.spec.model_id, ds, bbox))
            finally:
                Path(target).unlink(missing_ok=True)

        if not fields:
            raise RuntimeError(f"ECMWF {self.spec.model_id}: empty fetch")
        if len(fields) == 1:
            return fields[0]
        return WindField.concat_time(fields)


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
