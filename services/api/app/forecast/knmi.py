"""KNMI HARMONIE-AROME GRIB source (requires KNMI_API_KEY)."""

from __future__ import annotations

import logging
import os
from datetime import datetime

from ..metocean import WindField
from ..schemas import BBox
from .grib_source import (
    GribSource,
    SourceSpec,
    assemble_windfield,
    grib_bytes_to_uv,
    http_get,
    register,
)

logger = logging.getLogger(__name__)

_KNMI_BASE = "https://api.dataplatform.knmi.nl/open-data/v1"
_DATASET = "harmonie_arome_cy43_p1"
_DATASET_VERSION = "0.2"
_HARMONIE_STEPS = tuple(range(0, 49, 1))


def _knmi_headers() -> dict[str, str]:
    key = os.environ.get("KNMI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("KNMI_API_KEY not set")
    return {"Authorization": key}


class KnmiHarmonieSource(GribSource):
    def __init__(self, spec: SourceSpec):
        self.spec = spec

    def fetch(self, bbox: BBox, cycle: datetime, steps: list[int]) -> WindField:
        # KNMI open-data listing is file-based; ``steps`` is accepted for signature
        # parity but the listing filter is cycle-stamp based.
        _ = steps
        headers = _knmi_headers()
        # List latest files for the cycle, then download matching U/V GRIB.
        list_url = (
            f"{_KNMI_BASE}/datasets/{_DATASET}/versions/{_DATASET_VERSION}/files"
            f"?maxKeys=50&sorting=desc"
        )
        import json  # noqa: PLC0415

        listing = json.loads(http_get(list_url, headers=headers).decode("utf-8"))
        files = listing.get("files") or listing.get("Files") or []
        if not files:
            raise RuntimeError("KNMI HARMONIE: no files listed")

        # Prefer files matching the cycle stamp.
        stamp = cycle.strftime("%Y%m%d%H")
        candidates = [
            f for f in files if stamp in str(f.get("filename") or f.get("name") or "")
        ] or files[:4]

        per_step: list[tuple[float, object, object]] = []
        lats = lons = None
        for meta in candidates[:8]:
            fname = meta.get("filename") or meta.get("name")
            if not fname:
                continue
            # Skip non-wind files when possible.
            low = str(fname).lower()
            if "ugrd" not in low and "u_10" not in low and "wind" not in low and "10u" not in low:
                if "vgrd" not in low and "v_10" not in low and "10v" not in low:
                    continue
            get_url = (
                f"{_KNMI_BASE}/datasets/{_DATASET}/versions/{_DATASET_VERSION}"
                f"/files/{fname}/url"
            )
            try:
                url_payload = json.loads(http_get(get_url, headers=headers).decode("utf-8"))
                dl = url_payload.get("temporaryDownloadUrl") or url_payload.get("url")
                if not dl:
                    continue
                raw = http_get(dl)
                la, lo, u2d, v2d, te = grib_bytes_to_uv(raw)
                if lats is None:
                    lats, lons = la, lo
                per_step.append((te, u2d, v2d))
            except Exception as exc:  # noqa: BLE001
                logger.warning("KNMI file %s failed: %s", fname, exc)
                continue

        if not per_step or lats is None or lons is None:
            raise RuntimeError("KNMI HARMONIE: no GRIB steps fetched")

        field = assemble_windfield(self.spec.model_id, per_step, lats, lons)
        return field.subset_bbox(bbox)


if os.environ.get("KNMI_API_KEY", "").strip():
    register(
        KnmiHarmonieSource(
            SourceSpec(
                model_id="harmonie",
                connector="knmi-harmonie",
                resolution_deg=0.02,
                cycle_hours=(0, 6, 12, 18),
                step_hours=_HARMONIE_STEPS,
                publish_delay_h=3.0,
                api_key_env="KNMI_API_KEY",
            )
        )
    )
