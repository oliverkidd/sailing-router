"""Polar table parsing + interpolation."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import numpy as np

_DELIM = re.compile(r"[,;\t]|\s{2,}| +")
_HEADER_LABELS = {"twa/tws", "twa\\tws", "tws/twa", "twa", "tws", "angle", "speed"}


def _split(line: str) -> list[str]:
    line = line.strip()
    for delim in (",", ";", "\t"):
        if delim in line:
            return [c.strip() for c in line.split(delim)]
    return [c for c in re.split(r"\s+", line) if c != ""]


def _is_numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _detect_format(text: str) -> str:
    """Return 'matrix', 'pairwise', or 'unknown'."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("!")]
    if not lines:
        return "unknown"

    first = _split(lines[0])
    first_cell = first[0].lower().replace("\\", "/") if first else ""

    # Matrix: header label in first cell (TWA/TWS, TWA\TWS, etc.) or non-numeric header row.
    if first_cell in _HEADER_LABELS or (first and not _is_numeric(first[0])):
        return "matrix"

    # Pairwise Expedition: each line starts with TWS then (TWA, BSP) pairs.
    numeric_lines = 0
    pairwise_lines = 0
    for ln in lines[:15]:
        cells = _split(ln)
        if len(cells) < 7:
            continue
        if not _is_numeric(cells[0]):
            continue
        numeric_lines += 1
        # Odd count after TWS => pairs; even count after TWS => likely matrix row
        rest = cells[1:]
        if len(rest) % 2 == 0 and all(_is_numeric(c) for c in rest):
            pairwise_lines += 1

    if pairwise_lines >= 2 and pairwise_lines >= numeric_lines * 0.5:
        return "pairwise"

    # Default: if first row looks like header with numeric TWA columns, matrix.
    if first and not _is_numeric(first[0]):
        return "matrix"

    return "matrix"


@dataclass
class PolarTable:
    tws: np.ndarray
    twa: np.ndarray
    speed: np.ndarray
    # Quantised caches. A long race hits the same TWS hundreds of thousands of
    # times; the table is immutable so a 0.05 kn / 0.25° bucket is safe.
    _speed_cache: dict[tuple[int, int], float] = field(
        default_factory=dict, repr=False, compare=False
    )
    _vmg_cache: dict[tuple[int, bool], tuple[float, float]] = field(
        default_factory=dict, repr=False, compare=False
    )

    @classmethod
    def from_csv(cls, text: str) -> PolarTable:
        rows = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        if len(rows) < 2:
            raise ValueError("Polar CSV needs a header row and at least one data row")

        header = _split(rows[0])
        twa = np.array([float(x) for x in header[1:]], dtype=float)

        tws_list: list[float] = []
        speed_rows: list[list[float]] = []
        for ln in rows[1:]:
            cells = _split(ln)
            if len(cells) < 2:
                continue
            tws_list.append(float(cells[0]))
            vals = [float(x) for x in cells[1 : 1 + len(twa)]]
            if len(vals) < len(twa):
                vals += [0.0] * (len(twa) - len(vals))
            speed_rows.append(vals)

        tws = np.array(tws_list, dtype=float)
        speed = np.array(speed_rows, dtype=float)

        if speed.shape != (tws.size, twa.size):
            raise ValueError(
                f"Polar shape mismatch: got {speed.shape}, expected {(tws.size, twa.size)}"
            )

        ti = np.argsort(tws)
        ai = np.argsort(twa)
        return cls(tws=tws[ti], twa=twa[ai], speed=speed[np.ix_(ti, ai)])

    @classmethod
    def from_bytes(cls, data: bytes) -> PolarTable:
        text = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8-sig").read()
        fmt = _detect_format(text)
        if fmt == "pairwise":
            return cls.from_pol(text)
        if fmt == "matrix":
            return cls.from_csv(text)
        raise ValueError("Unrecognized polar format (expected matrix CSV or Expedition pairwise .pol)")

    @classmethod
    def from_pol(cls, text: str) -> PolarTable:
        tws_list: list[float] = []
        speed_rows: list[tuple[list[float], list[float]]] = []
        twa_set: set[float] = set()

        for ln in text.splitlines():
            s = ln.strip()
            if not s or s.startswith("!") or s.lower().startswith("pol"):
                continue
            cells = _split(ln)
            if len(cells) < 7 or not _is_numeric(cells[0]):
                continue
            tws = float(cells[0])
            pairs = cells[1:]
            if len(pairs) % 2 != 0:
                pairs = pairs[:-1]
            twas: list[float] = []
            speeds: list[float] = []
            for k in range(0, len(pairs), 2):
                twas.append(float(pairs[k]))
                speeds.append(float(pairs[k + 1]))
            tws_list.append(tws)
            for a in twas:
                twa_set.add(a)
            speed_rows.append((twas, speeds))

        if not tws_list:
            raise ValueError("No TWS curves found in pairwise .pol file")

        twa = np.array(sorted(twa_set), dtype=float)
        tws = np.array(tws_list, dtype=float)
        speed = np.zeros((tws.size, twa.size), dtype=float)
        for i, (twas, speeds) in enumerate(speed_rows):
            lut = dict(zip(twas, speeds, strict=False))
            for j, a in enumerate(twa):
                speed[i, j] = lut.get(float(a), 0.0)

        ti = np.argsort(tws)
        ai = np.argsort(twa)
        return cls(tws=tws[ti], twa=twa[ai], speed=speed[np.ix_(ti, ai)])

    def speed_for(self, tws: float, twa: float) -> float:
        twa = abs(twa)
        if twa > 180.0:
            twa = 360.0 - twa
        if twa < self.twa[0]:
            return 0.0

        # 0.05 kn × 0.25° buckets — well below polar/table resolution.
        # Always evaluate at the bucket centre so the first query in a bucket
        # cannot bias later ones (reusing one Boat across eval cases used to
        # change routes depending on case order).
        sk = (int(round(tws * 20.0)), int(round(twa * 4.0)))
        hit = self._speed_cache.get(sk)
        if hit is not None:
            return hit
        tws = sk[0] / 20.0
        twa = sk[1] / 4.0

        # Below the lowest tabulated TWS, scale linearly toward zero (no phantom speed).
        low_air_factor = 1.0
        if tws < self.tws[0]:
            if self.tws[0] <= 0:
                return 0.0
            low_air_factor = max(0.0, min(1.0, tws / self.tws[0]))

        t = float(np.clip(tws, self.tws[0], self.tws[-1]))
        a = float(np.clip(twa, self.twa[0], self.twa[-1]))

        it = int(np.searchsorted(self.tws, t) - 1)
        it = max(0, min(it, self.tws.size - 2))
        ia = int(np.searchsorted(self.twa, a) - 1)
        ia = max(0, min(ia, self.twa.size - 2))

        t0, t1 = self.tws[it], self.tws[it + 1]
        a0, a1 = self.twa[ia], self.twa[ia + 1]
        ft = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
        fa = 0.0 if a1 == a0 else (a - a0) / (a1 - a0)

        s00 = self.speed[it, ia]
        s01 = self.speed[it, ia + 1]
        s10 = self.speed[it + 1, ia]
        s11 = self.speed[it + 1, ia + 1]
        s0 = s00 * (1 - fa) + s01 * fa
        s1 = s10 * (1 - fa) + s11 * fa
        val = float(max(0.0, (s0 * (1 - ft) + s1 * ft) * low_air_factor))
        self._speed_cache[sk] = val
        return val

    def best_vmg_angle(self, tws: float, upwind: bool) -> tuple[float, float]:
        vk = (int(round(tws * 20.0)), upwind)
        hit = self._vmg_cache.get(vk)
        if hit is not None:
            return hit
        tws = vk[0] / 20.0

        if upwind:
            lo, hi = float(self.twa[0]), 90.0
        else:
            lo, hi = 90.0, float(self.twa[-1])
        angles = np.linspace(lo, hi, 181)
        # One TWS slice across the tabulated TWA grid, then sample the 181 angles.
        low_air_factor = 1.0
        if tws < self.tws[0]:
            if self.tws[0] <= 0:
                out = (float(angles[0]), 0.0)
                self._vmg_cache[vk] = out
                return out
            low_air_factor = max(0.0, min(1.0, tws / self.tws[0]))
        t = float(np.clip(tws, self.tws[0], self.tws[-1]))
        it = int(np.searchsorted(self.tws, t) - 1)
        it = max(0, min(it, self.tws.size - 2))
        t0, t1 = self.tws[it], self.tws[it + 1]
        ft = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
        bsp_grid = (self.speed[it] * (1.0 - ft) + self.speed[it + 1] * ft) * low_air_factor
        bsp = np.interp(angles, self.twa, bsp_grid)
        vmg = bsp * np.abs(np.cos(np.radians(angles)))
        i = int(np.argmax(vmg))
        out = (float(angles[i]), float(vmg[i]))
        self._vmg_cache[vk] = out
        return out
