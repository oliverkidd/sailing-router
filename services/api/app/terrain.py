"""Terrain elevation from pre-clipped Copernicus DEM GLO-30 GeoTIFFs.

Provides point elevation (for /elevation and the map hillshade) and feeds the DEM
GeoTIFF to WindNinja (M6). rasterio is an optional native dependency; when it or
the DEM is absent we degrade gracefully so the default Open-Meteo routing path
never hard-fails.

Opens the full-region VRT when present, otherwise every region mosaic
and leftover 1° tiles under data/dem/.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .config import DATA_DIR

_DEM_DIR = DATA_DIR / "dem"

# Copernicus GLO-30 nodata sentinel (and a safety floor for masked ocean).
_NODATA_SENTINEL = -32767.0
_NODATA_FLOOR = -1000.0


class TerrainProvider:
    def __init__(self, dem_path: str | Path | None = None):
        self._dem_path = Path(dem_path) if dem_path else self._default_dem()
        self._ds = None
        self._extra: list = []  # additional open datasets (other mosaics / tiles)
        self._open()

    @staticmethod
    def _list_tifs() -> list[Path]:
        if not _DEM_DIR.exists():
            return []
        return sorted(_DEM_DIR.glob("*.tif")) + sorted(_DEM_DIR.glob("*.tiff"))

    @staticmethod
    def _list_mosaics() -> list[Path]:
        if not _DEM_DIR.exists():
            return []
        return sorted(_DEM_DIR.glob("region_dem*.tif")) + sorted(
            _DEM_DIR.glob("region_dem*.tiff")
        )

    @staticmethod
    def _default_dem() -> Path | None:
        """Prefer the full-region VRT; otherwise a mosaic or the first tile."""
        vrt = _DEM_DIR / "region_dem.vrt"
        if vrt.exists():
            return vrt
        mosaics = TerrainProvider._list_mosaics()
        if mosaics:
            return mosaics[0]
        tifs = TerrainProvider._list_tifs()
        return tifs[0] if tifs else None

    def _open(self) -> None:
        try:
            import rasterio  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return

        if self._dem_path is not None and Path(self._dem_path).exists():
            try:
                self._ds = rasterio.open(self._dem_path)
            except Exception:  # noqa: BLE001
                self._ds = None

        primary = self._dem_path.resolve() if self._dem_path else None
        opened: set[Path] = set()
        if primary is not None:
            opened.add(primary)

        # Full-region VRT already mosaics every 1° COG — do not open them again.
        if self._dem_path is not None and self._dem_path.name == "region_dem.vrt":
            return

        # Open every other mosaic first (clip leftovers), then leftover 1° tiles.
        for f in self._list_mosaics() + self._list_tifs():
            try:
                resolved = f.resolve()
            except Exception:  # noqa: BLE001
                continue
            if resolved in opened:
                continue
            try:
                self._extra.append(rasterio.open(f))
                opened.add(resolved)
            except Exception:  # noqa: BLE001
                continue

    def _datasets(self) -> list:
        out = []
        if self._ds is not None:
            out.append(self._ds)
        out.extend(self._extra)
        return out

    @property
    def available(self) -> bool:
        return bool(self._datasets())

    def footprints(self) -> list[dict[str, Any]]:
        """Bounding boxes of every open DEM raster (WGS84 lon/lat)."""
        out: list[dict[str, Any]] = []
        for ds in self._datasets():
            try:
                b = ds.bounds
                name = Path(getattr(ds, "name", "") or "").name or "dem"
                out.append(
                    {
                        "name": name,
                        "lon_min": float(b.left),
                        "lat_min": float(b.bottom),
                        "lon_max": float(b.right),
                        "lat_max": float(b.top),
                    }
                )
            except Exception:  # noqa: BLE001
                continue
        return out

    def covers(self, lat: float, lon: float) -> bool:
        """True when any open DEM bounds contain (lat, lon)."""
        for ds in self._datasets():
            try:
                b = ds.bounds
            except Exception:  # noqa: BLE001
                continue
            if b.left <= lon <= b.right and b.bottom <= lat <= b.top:
                return True
        return False

    @staticmethod
    def _read_elev(ds, lat: float, lon: float) -> float | None:
        """Windowed sample; None when out of bounds or nodata."""
        import rasterio  # noqa: PLC0415

        try:
            row, col = ds.index(lon, lat)
        except Exception:  # noqa: BLE001
            return None
        if row < 0 or col < 0 or row >= ds.height or col >= ds.width:
            return None
        try:
            window = rasterio.windows.Window(col, row, 1, 1)
            val = float(ds.read(1, window=window)[0, 0])
        except Exception:  # noqa: BLE001
            return None
        if math.isnan(val) or val <= _NODATA_FLOOR or val == _NODATA_SENTINEL:
            return None
        nodata = ds.nodata
        if nodata is not None and val == float(nodata):
            return None
        return val

    def elevation(self, lat: float, lon: float) -> float | None:
        """Elevation in metres from the DEM, or None when no raster covers the point."""
        for ds in self._datasets():
            val = self._read_elev(ds, lat, lon)
            if val is not None:
                return val
        return None


_default_provider: TerrainProvider | None = None


def get_terrain() -> TerrainProvider:
    global _default_provider
    if _default_provider is None:
        _default_provider = TerrainProvider()
    return _default_provider
