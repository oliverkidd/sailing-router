"""Polar table parsing + interpolation.

Polar CSV in ORC / Expedition format: the first row is a header of TWA angles
(its first cell is an ignored label), and each subsequent row starts with a TWS
value followed by boat speeds (knots) for each TWA.

    twa/tws , 40 , 52 , 60 , 75 , 90 , 110 , 120 , 135 , 150 , 165 , 180
    6       , 0  , 5.1, 5.6, 6.0, 6.1, 6.0 , 5.9 , 5.4 , 4.7 , 3.9 , 3.4
    8       , 0  , 6.0, 6.6, 7.1, 7.2, 7.1 , 7.0 , 6.4 , 5.7 , 4.9 , 4.3
    ...

Delimiters comma / semicolon / tab / whitespace are all accepted.

Conventions:
- TWA is 0..180 (symmetric port/starboard); speeds in knots.
- `speed_for(tws, twa)` clamps to the axes and bilinearly interpolates.
- No-go returns 0: below the upwind limit or above the deep-run limit the polar
  cells are themselves 0, and any non-positive interpolation is clamped to 0.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

import numpy as np

_DELIM = re.compile(r"[,;\t]|\s{2,}| +")


def _split(line: str) -> list[str]:
    line = line.strip()
    # Prefer explicit delimiters; fall back to whitespace.
    for delim in (",", ";", "\t"):
        if delim in line:
            return [c.strip() for c in line.split(delim)]
    return [c for c in re.split(r"\s+", line) if c != ""]


@dataclass
class PolarTable:
    tws: np.ndarray  # shape (n_tws,), ascending
    twa: np.ndarray  # shape (n_twa,), ascending, 0..180
    speed: np.ndarray  # shape (n_tws, n_twa), knots

    @classmethod
    def from_csv(cls, text: str) -> PolarTable:
        rows = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        if len(rows) < 2:
            raise ValueError("Polar CSV needs a header row and at least one data row")

        header = _split(rows[0])
        # Header first cell is a label (e.g. "twa/tws"); the rest are TWA angles.
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

        # Sort axes ascending (keeps interpolation simple).
        ti = np.argsort(tws)
        ai = np.argsort(twa)
        tws, twa = tws[ti], twa[ai]
        speed = speed[np.ix_(ti, ai)]

        return cls(tws=tws, twa=twa, speed=speed)

    @classmethod
    def from_bytes(cls, data: bytes) -> PolarTable:
        return cls.from_csv(io.TextIOWrapper(io.BytesIO(data), encoding="utf-8-sig").read())

    def speed_for(self, tws: float, twa: float) -> float:
        """Bilinear boat speed (knots) at (tws, twa). No-go -> 0."""
        twa = abs(twa)
        if twa > 180.0:
            twa = 360.0 - twa  # fold onto 0..180

        # Below the smallest tabulated TWA (upwind no-go) => 0.
        if twa < self.twa[0]:
            return 0.0

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
        val = s0 * (1 - ft) + s1 * ft
        return float(max(0.0, val))

    def best_vmg_angle(self, tws: float, upwind: bool) -> tuple[float, float]:
        """Return (twa, vmg_knots) that maximizes velocity-made-good at this TWS.

        Upwind maximizes bsp*cos(twa) over TWA in [twa_min, 90); downwind
        maximizes bsp*|cos(twa)| over (90, twa_max].
        """
        # Fine sweep over the TWA range for a smooth optimum.
        if upwind:
            lo, hi = float(self.twa[0]), 90.0
        else:
            lo, hi = 90.0, float(self.twa[-1])
        angles = np.linspace(lo, hi, 181)
        best_twa, best_vmg = angles[0], -1.0
        for ang in angles:
            bsp = self.speed_for(tws, float(ang))
            vmg = bsp * abs(np.cos(np.radians(ang)))
            if vmg > best_vmg:
                best_vmg, best_twa = vmg, float(ang)
        return best_twa, float(best_vmg)
