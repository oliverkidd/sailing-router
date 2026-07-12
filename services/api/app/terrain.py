"""Terrain elevation from a pre-clipped Copernicus DEM GLO-30 GeoTIFF.

Provides point elevation (for /elevation and the map hillshade) and feeds the DEM
GeoTIFF to WindNinja (M6). rasterio is an optional native dependency; when it or
the DEM is absent we degrade gracefully to sea-level (0 m) so the default
Open-Meteo routing path never hard-fails.
"""

from __future__ import annotations

from pathlib import Path

from .config import DATA_DIR

_DEM_DIR = DATA_DIR / "dem"


class TerrainProvider:
    def __init__(self, dem_path: str | Path | None = None):
        self._dem_path = Path(dem_path) if dem_path else self._default_dem()
        self._ds = None
        self._band = None
        self._open()

    @staticmethod
    def _default_dem() -> Path | None:
        if not _DEM_DIR.exists():
            return None
        for f in sorted(_DEM_DIR.glob("*.tif")) + sorted(_DEM_DIR.glob("*.tiff")):
            return f
        return None

    def _open(self) -> None:
        if self._dem_path is None or not Path(self._dem_path).exists():
            return
        try:
            import rasterio  # noqa: PLC0415

            self._ds = rasterio.open(self._dem_path)
        except Exception:  # noqa: BLE001
            # rasterio not installed or file unreadable -> graceful fallback.
            self._ds = None

    @property
    def available(self) -> bool:
        return self._ds is not None

    def elevation(self, lat: float, lon: float) -> float | None:
        """Elevation in metres from the DEM, or None when no raster is loaded."""
        if self._ds is None:
            return None
        try:
            import rasterio  # noqa: PLC0415

            row, col = self._ds.index(lon, lat)
            # Windowed read — never materialise the full DEM band into RAM.
            window = rasterio.windows.Window(col, row, 1, 1)
            val = self._ds.read(1, window=window)[0, 0]
            return float(val)
        except Exception:  # noqa: BLE001
            return None


_default_provider: TerrainProvider | None = None


def get_terrain() -> TerrainProvider:
    global _default_provider
    if _default_provider is None:
        _default_provider = TerrainProvider()
    return _default_provider
