"""Fetch small European fixtures for fully-offline tests + demo.

Run via `make fetch-fixtures`. Downloads a small Open-Meteo wind clip over an
Aegean bbox and caches it, and writes an offline FES current clip. Everything is
European and free; tests themselves never hit the network (they use the checked-in
JSON fixture), but this seeds the on-disk cache so the app runs offline afterwards.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .. import cache
from ..currents import get_current_provider
from ..forecast import get_provider
from ..schemas import BBox

# Broad Aegean box so any contained sample course routes fully offline.
AEGEAN = BBox(lat_min=35.0, lon_min=23.0, lat_max=38.5, lon_max=26.5)


def main() -> None:
    now = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    print("Fetching Open-Meteo wind fixtures (ECMWF, ICON) over the Aegean…")
    for model_id in ("ecmwf", "icon"):
        try:
            field = get_provider("open-meteo").fetch(model_id, AEGEAN, None, 72)
            key = cache.wind_key(model_id, AEGEAN, None)
            cache.save_wind(key, field)
            print(f"  {model_id}: cached {field.n_times} steps -> {key}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {model_id}: FAILED ({exc})")

    print("Writing offline FES tidal current clip…")
    try:
        cf = get_current_provider("fes").fetch(AEGEAN, now, now + timedelta(hours=72))
        key = cache.current_key("fes", AEGEAN, now.isoformat())
        cache.save_current(key, cf)
        print(f"  fes: cached -> {key}")
    except Exception as exc:  # noqa: BLE001
        print(f"  fes: FAILED ({exc})")

    print("Done. The app can now route offline over the Aegean bbox.")


if __name__ == "__main__":
    main()
