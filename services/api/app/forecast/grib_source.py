"""GRIB source abstraction + shared download/parse helpers."""

from __future__ import annotations

import bz2
import logging
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import xarray as xr

from ..metocean import WindField
from ..schemas import BBox

logger = logging.getLogger(__name__)

_MAX_RETRIES = 4
_RETRY_STATUSES = {429, 502, 503, 504}


class ForecastRateLimitError(RuntimeError):
    """Upstream forecast CDN rejected the request (HTTP 429)."""

    def __init__(self, model_id: str, detail: str = ""):
        self.model_id = model_id
        msg = (
            f"rate limited by {model_id} open data"
            " — try again shortly or use GFS/ICON"
        )
        if detail:
            msg = f"{msg} ({detail})"
        super().__init__(msg)


@dataclass(frozen=True)
class SourceSpec:
    model_id: str
    connector: str
    resolution_deg: float
    cycle_hours: tuple[int, ...]
    step_hours: tuple[int, ...]
    publish_delay_h: float
    api_key_env: str | None = None


class GribSource(ABC):
    """A pluggable GRIB wind source. Adapters return a *native* WindField.

    Canonical (500 m + hourly) normalization happens in field_service, not here.
    """

    spec: SourceSpec

    @abstractmethod
    def fetch(self, bbox: BBox, cycle: datetime, steps: list[int]) -> WindField:
        """Download the given native step offsets and return a native-grid WindField."""
        raise NotImplementedError

    def latest_cycle(self, now: datetime | None = None) -> datetime:
        """Newest cycle_hours slot older than now - publish_delay_h."""
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        cutoff = now - timedelta(hours=self.spec.publish_delay_h)
        best: datetime | None = None
        # Search back up to 3 days of cycle slots.
        day0 = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
        for day_back in range(0, 4):
            day = day0 - timedelta(days=day_back)
            for h in self.spec.cycle_hours:
                c = day.replace(hour=h)
                if c <= cutoff and (best is None or c > best):
                    best = c
        if best is None:
            best = day0.replace(hour=min(self.spec.cycle_hours)) - timedelta(days=1)
        return best

    def steps_for_window(
        self, cycle: datetime, start: datetime, end: datetime
    ) -> list[int]:
        """Native step offsets whose valid-times bracket [start, end].

        Includes one step on each side of the window so hourly interpolation at
        the edges is exact. No artificial thinning — cadence is model-native.
        """
        if cycle.tzinfo is None:
            cycle = cycle.replace(tzinfo=UTC)
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        off_start = (start - cycle).total_seconds() / 3600.0
        off_end = (end - cycle).total_seconds() / 3600.0
        steps = list(self.spec.step_hours)
        if not steps:
            return [0]
        inside = [s for s in steps if off_start <= s <= off_end]
        lower_candidates = [s for s in steps if s <= off_start]
        upper_candidates = [s for s in steps if s >= off_end]
        lower = max(lower_candidates) if lower_candidates else steps[0]
        upper = min(upper_candidates) if upper_candidates else steps[-1]
        return sorted({lower, *inside, upper})

    def steps_for_horizon(self, horizon_h: int) -> list[int]:
        """All native steps covering [0, horizon_h] (no thinning)."""
        steps = [s for s in self.spec.step_hours if s <= horizon_h]
        return steps or [0]



# --- Shared HTTP / GRIB helpers ---------------------------------------------

_DEFAULT_STEP_WORKERS = 6


def _short_url(url: str) -> str:
    """Host + path for logs (omit query strings that may contain API keys)."""
    from urllib.parse import urlparse  # noqa: PLC0415

    p = urlparse(url)
    return f"{p.netloc}{p.path}"


def _http_error_kind(exc: BaseException) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def http_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 120.0,
    client: httpx.Client | None = None,
) -> bytes:
    short = _short_url(url)
    t0 = time.perf_counter()
    last_exc: Exception | None = None
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=timeout, follow_redirects=True)
    assert client is not None
    try:
        for attempt in range(_MAX_RETRIES):
            try:
                if attempt == 0:
                    logger.info("forecast GET %s (timeout=%.0fs)", short, timeout)
                else:
                    logger.warning(
                        "forecast GET retry %d/%d %s after [%s] %s",
                        attempt + 1,
                        _MAX_RETRIES,
                        short,
                        _http_error_kind(last_exc) if last_exc else "?",
                        last_exc,
                    )
                resp = client.get(url, headers=headers)
                if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                    logger.warning(
                        "forecast GET %s HTTP %d (attempt %d/%d, backing off)",
                        short,
                        resp.status_code,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    time.sleep(1.5 * (2**attempt))
                    continue
                resp.raise_for_status()
                elapsed_ms = (time.perf_counter() - t0) * 1000
                logger.info(
                    "forecast GET %s OK %d bytes %.0fms%s",
                    short,
                    len(resp.content),
                    elapsed_ms,
                    f" (attempt {attempt + 1})" if attempt > 0 else "",
                )
                return resp.content
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    logger.warning(
                        "forecast GET %s [%s] %s (attempt %d/%d, backing off)",
                        short,
                        _http_error_kind(exc),
                        exc,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    time.sleep(1.5 * (2**attempt))
                    continue
                elapsed_ms = (time.perf_counter() - t0) * 1000
                logger.error(
                    "forecast GET %s FAILED after %d attempts %.0fms: [%s] %s",
                    short,
                    _MAX_RETRIES,
                    elapsed_ms,
                    _http_error_kind(exc),
                    exc,
                )
                raise
    finally:
        if owns_client:
            client.close()
    if last_exc:
        raise last_exc
    raise RuntimeError(f"GET failed: {url}")


def http_get_range(
    url: str,
    start: int,
    end: int,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 120.0,
    client: httpx.Client | None = None,
) -> bytes:
    short = _short_url(url)
    logger.info("forecast GET range %s bytes=%d-%d", short, start, end)
    hdrs = dict(headers or {})
    hdrs["Range"] = f"bytes={start}-{end}"
    return http_get(url, headers=hdrs, timeout=timeout, client=client)


def map_steps_parallel(
    steps: list[int],
    fetch_one,
    *,
    max_workers: int = _DEFAULT_STEP_WORKERS,
) -> list[tuple[int, object]]:
    """Run ``fetch_one(step)`` across steps with a bounded thread pool.

    Returns ``[(step, result), ...]`` for successful steps, ordered by step.
    Failed steps are logged and omitted (caller decides whether enough remain).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415

    if not steps:
        return []
    if len(steps) == 1:
        try:
            return [(steps[0], fetch_one(steps[0]))]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "forecast step %s failed: [%s] %s",
                steps[0],
                _http_error_kind(exc),
                exc,
            )
            return []

    workers = max(1, min(max_workers, len(steps)))
    out: dict[int, object] = {}
    failed: list[tuple[int, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_one, s): s for s in steps}
        for fut in as_completed(futs):
            step = futs[fut]
            try:
                out[step] = fut.result()
            except Exception as exc:  # noqa: BLE001
                failed.append((step, f"[{_http_error_kind(exc)}] {exc}"))
                logger.warning("forecast step %s failed: [%s] %s", step, _http_error_kind(exc), exc)
    if failed:
        logger.warning(
            "forecast parallel fetch: %d/%d steps failed (%s)",
            len(failed),
            len(steps),
            ", ".join(f"f{step:03d}" for step, _ in failed[:6])
            + ("…" if len(failed) > 6 else ""),
        )
    return [(s, out[s]) for s in sorted(out)]


def bunzip2(raw: bytes) -> bytes:
    return bz2.decompress(raw)


def parse_idx(idx_text: str) -> list[tuple[str, int]]:
    """Parse a NOAA-style .idx file into (record_key, byte_offset) pairs.

    Each line looks like:
      1:0:d=2026071100:UGRD:10 m above ground:3 hour fcst:
    The second colon-field is the byte offset.
    """
    records: list[tuple[str, int]] = []
    for line in idx_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) < 5:
            continue
        try:
            offset = int(parts[1])
        except ValueError:
            continue
        # Key = everything after the offset field (var + level + ...).
        key = ":".join(parts[2:])
        records.append((key, offset))
    return records


def find_idx_range(
    records: list[tuple[str, int]],
    needle: str,
) -> tuple[int, int] | None:
    """Return (start, end_inclusive) byte range for the first record whose key contains needle."""
    for i, (key, offset) in enumerate(records):
        if needle in key:
            start = offset
            end = records[i + 1][1] - 1 if i + 1 < len(records) else None
            if end is None:
                # Unknown length — caller must handle open-ended range carefully.
                return start, -1
            return start, end
    return None


def _coords_1d(da: xr.DataArray, names: tuple[str, ...]) -> np.ndarray:
    for name in names:
        if name in da.coords:
            return np.asarray(da.coords[name].values, dtype=float)
        if name in da.dims:
            return np.asarray(da[name].values, dtype=float)
    raise KeyError(f"No coordinate among {names} in {list(da.coords)} / {list(da.dims)}")


def _valid_time_epoch(ds: xr.Dataset) -> float:
    for name in ("valid_time", "time"):
        if name in ds.coords:
            val = ds.coords[name].values
            # May be scalar or array.
            arr = np.atleast_1d(val)
            t0 = arr[0]
            if np.issubdtype(np.asarray(t0).dtype, np.datetime64):
                return float(np.datetime64(t0, "s").astype("datetime64[s]").astype(np.int64))
            # pandas Timestamp / cftime
            try:
                return datetime.fromisoformat(str(t0)[:19]).replace(tzinfo=UTC).timestamp()
            except Exception:  # noqa: BLE001
                pass
    raise KeyError("No valid_time/time coordinate in dataset")


def grib_bytes_to_component(
    raw: bytes,
    *,
    which: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Parse a single-component GRIB2 (U or V) -> (lats, lons, field2d, valid_epoch).

    ``which`` is ``"u"`` or ``"v"``. Used when U/V arrive as separate downloads (GFS/GEM/DWD).
    """
    which = which.lower()
    if which not in ("u", "v"):
        raise ValueError("which must be 'u' or 'v'")
    short_names = ("10u", "u10", "u") if which == "u" else ("10v", "v10", "v")
    filters = (
        ({"shortName": "10u"}, {"shortName": "u10"}, {"typeOfLevel": "heightAboveGround", "level": 10, "shortName": "u"})
        if which == "u"
        else (
            {"shortName": "10v"},
            {"shortName": "v10"},
            {"typeOfLevel": "heightAboveGround", "level": 10, "shortName": "v"},
        )
    )

    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as f:
        f.write(raw)
        path = Path(f.name)
    try:
        da = None
        last_err: Exception | None = None
        for filter_keys in filters:
            try:
                ds = xr.open_dataset(
                    path,
                    engine="cfgrib",
                    backend_kwargs={"filter_by_keys": filter_keys, "indexpath": ""},
                )
                name = next(iter(ds.data_vars))
                da = ds[name]
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue
        if da is None:
            try:
                ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
                names = {n.lower(): n for n in ds.data_vars}
                for cand in short_names:
                    if cand in names:
                        da = ds[names[cand]]
                        break
                # Last resort: sole data var in a single-message file.
                if da is None and len(ds.data_vars) == 1:
                    da = ds[next(iter(ds.data_vars))]
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        if da is None:
            raise RuntimeError(f"Could not extract 10{which} from GRIB: {last_err}")

        lats = _coords_1d(da, ("latitude", "lat", "y"))
        lons = _coords_1d(da, ("longitude", "lon", "x"))
        arr = np.asarray(da.values, dtype=float)
        while arr.ndim > 2:
            arr = arr[0]
        te = _valid_time_epoch(da.to_dataset(name=f"{which}10"))
        if lats.size >= 2 and lats[0] > lats[-1]:
            lats = lats[::-1].copy()
            arr = arr[::-1, :]
        if lons.size >= 2 and lons[0] > lons[-1]:
            lons = lons[::-1].copy()
            arr = arr[:, ::-1]
        return lats, lons, np.nan_to_num(arr, nan=0.0), te
    finally:
        path.unlink(missing_ok=True)


def grib_bytes_to_uv(
    raw: bytes,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, float]:
    """Parse GRIB2 bytes containing both U and V -> (lats, lons, u2d, v2d, valid_epoch)."""
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as f:
        f.write(raw)
        path = Path(f.name)
    try:
        # Prefer opening as a combined file first.
        try:
            ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
            names = {n.lower(): n for n in ds.data_vars}
            for cand_u, cand_v in (("10u", "10v"), ("u10", "v10"), ("u", "v")):
                if cand_u in names and cand_v in names:
                    u_da = ds[names[cand_u]]
                    v_da = ds[names[cand_v]]
                    lats = _coords_1d(u_da, ("latitude", "lat", "y"))
                    lons = _coords_1d(u_da, ("longitude", "lon", "x"))
                    u2d = np.asarray(u_da.values, dtype=float)
                    v2d = np.asarray(v_da.values, dtype=float)
                    while u2d.ndim > 2:
                        u2d = u2d[0]
                        v2d = v2d[0]
                    te = _valid_time_epoch(u_da.to_dataset(name="u10"))
                    if lats.size >= 2 and lats[0] > lats[-1]:
                        lats = lats[::-1].copy()
                        u2d = u2d[::-1, :]
                        v2d = v2d[::-1, :]
                    if lons.size >= 2 and lons[0] > lons[-1]:
                        lons = lons[::-1].copy()
                        u2d = u2d[:, ::-1]
                        v2d = v2d[:, ::-1]
                    return (
                        lats,
                        lons,
                        np.nan_to_num(u2d, nan=0.0),
                        np.nan_to_num(v2d, nan=0.0),
                        te,
                    )
        except Exception:  # noqa: BLE001
            pass

        # Fall back: try component extractors on the same blob (message may be U-only or V-only).
        la, lo, u2d, te = grib_bytes_to_component(raw, which="u")
        _, _, v2d, _ = grib_bytes_to_component(raw, which="v")
        return la, lo, u2d, v2d, te
    finally:
        path.unlink(missing_ok=True)



def dataset_to_windfield(model_id: str, ds: xr.Dataset, bbox: BBox) -> WindField:
    """Convert an xarray Dataset with u10/v10 (or 10u/10v) to a WindField, subset to bbox."""
    name_map = {n.lower(): n for n in ds.data_vars}
    u_name = next((name_map[k] for k in ("10u", "u10", "u") if k in name_map), None)
    v_name = next((name_map[k] for k in ("10v", "v10", "v") if k in name_map), None)
    if u_name is None or v_name is None:
        raise KeyError(f"Dataset missing u/v vars: {list(ds.data_vars)}")

    u_da = ds[u_name]
    v_da = ds[v_name]
    lats = _coords_1d(u_da, ("latitude", "lat", "y"))
    lons = _coords_1d(u_da, ("longitude", "lon", "x"))

    u = np.asarray(u_da.values, dtype=float)
    v = np.asarray(v_da.values, dtype=float)
    if u.ndim == 2:
        u = u[np.newaxis, ...]
        v = v[np.newaxis, ...]

    # Time axis.
    times: list[float] = []
    for name in ("valid_time", "time", "step"):
        if name in ds.coords:
            arr = np.atleast_1d(ds.coords[name].values)
            for t0 in arr:
                if np.issubdtype(np.asarray(t0).dtype, np.datetime64):
                    times.append(
                        float(np.datetime64(t0, "s").astype("datetime64[s]").astype(np.int64))
                    )
                else:
                    try:
                        times.append(
                            datetime.fromisoformat(str(t0)[:19]).replace(tzinfo=UTC).timestamp()
                        )
                    except Exception:  # noqa: BLE001
                        continue
            break
    if not times:
        times = [datetime.now(UTC).timestamp()]
    if len(times) == 1 and u.shape[0] > 1:
        # Expand single time — shouldn't happen, but keep shapes consistent.
        times = [times[0] + i * 3600 for i in range(u.shape[0])]
    if len(times) != u.shape[0]:
        times = times[: u.shape[0]]
        if len(times) < u.shape[0]:
            base = times[-1] if times else datetime.now(UTC).timestamp()
            while len(times) < u.shape[0]:
                times.append(base + (len(times)) * 3600)

    field = WindField.build(model_id, times, lats, lons, u, v)
    return field.subset_bbox(bbox)


def assemble_windfield(
    model_id: str,
    per_step: list[tuple[float, np.ndarray, np.ndarray]],
    lats: np.ndarray,
    lons: np.ndarray,
) -> WindField:
    """Stack (valid_epoch, u2d, v2d) steps into a WindField."""
    if not per_step:
        raise ValueError("No GRIB steps to assemble")
    times = np.array([t for t, _, _ in per_step], dtype=float)
    u10 = np.stack([u for _, u, _ in per_step], axis=0)
    v10 = np.stack([v for _, _, v in per_step], axis=0)
    return WindField.build(model_id, times, lats, lons, u10, v10)


# --- Registry ---------------------------------------------------------------

REGISTRY: dict[str, GribSource] = {}


def register(source: GribSource) -> None:
    REGISTRY[source.spec.model_id] = source


def get_source(model_id: str) -> GribSource:
    src = REGISTRY.get(model_id)
    if src is None:
        raise KeyError(f"No GRIB source registered for model '{model_id}'")
    return src


def register_all() -> None:
    """Import adapters so they self-register. Idempotent."""
    # Local imports to avoid cycles at module load.
    from . import dwd_icon, ecmwf, gem, gfs, knmi, meteofrance  # noqa: F401

    _ = (ecmwf, dwd_icon, gfs, gem, meteofrance, knmi)
