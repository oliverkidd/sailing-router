"""Disk-backed polar registry.

Uploaded polar CSVs are saved under the cache dir keyed by polar_id so the router
can look them up later (local-first: everything persists on disk). The bundled
sample polar is registered on first use so the UI works out of the box.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .config import CACHE_DIR, DATA_DIR
from .polar import PolarTable

_POLAR_DIR = CACHE_DIR / "polars"
_POLAR_DIR.mkdir(parents=True, exist_ok=True)

_SAMPLE_SRC = DATA_DIR / "sample-polars" / "sample_orc.csv"


def _path(polar_id: str) -> Path:
    return _POLAR_DIR / f"{polar_id}.csv"


def save_polar(name: str, csv_text: str) -> str:
    """Persist a polar CSV; returns a stable polar_id (name + content hash)."""
    digest = hashlib.sha1(csv_text.encode("utf-8")).hexdigest()[:8]
    safe = "".join(c for c in Path(name).stem if c.isalnum() or c in ("-", "_")) or "polar"
    polar_id = f"{safe}-{digest}"
    _path(polar_id).write_text(csv_text, encoding="utf-8")
    return polar_id


def get_polar(polar_id: str) -> PolarTable:
    p = _path(polar_id)
    if not p.exists():
        if polar_id == "sample" and _SAMPLE_SRC.exists():
            return PolarTable.from_csv(_SAMPLE_SRC.read_text(encoding="utf-8"))
        raise KeyError(f"Unknown polar_id: {polar_id}")
    return PolarTable.from_csv(p.read_text(encoding="utf-8"))


def ensure_sample() -> str:
    """Register the bundled sample polar and return its polar_id."""
    if _SAMPLE_SRC.exists():
        return save_polar("sample", _SAMPLE_SRC.read_text(encoding="utf-8"))
    raise FileNotFoundError(_SAMPLE_SRC)


def has_polar(polar_id: str) -> bool:
    return _path(polar_id).exists() or polar_id == "sample"
