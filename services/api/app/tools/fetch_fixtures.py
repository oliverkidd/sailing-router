"""Fetch small European fixtures for fully-offline tests + demo (GRIB sources)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..field_service import get_area_field
from ..geo.fetch import ensure_geo_context
from ..schemas import BBox, Course

AEGEAN_START = (36.2, 24.6)
AEGEAN_FINISH = (37.0, 24.6)


def main() -> None:
    now = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    print("Fetching GRIB wind fixtures (ECMWF, GFS) over the Aegean…")
    aegean = BBox(lat_min=35.0, lon_min=23.0, lat_max=38.5, lon_max=26.5)
    for model_id in ("ecmwf", "gfs"):
        try:
            field = get_area_field(
                model_id, aegean, start=now, end=now + timedelta(hours=72)
            )
            if field is None:
                print(f"  {model_id}: unavailable")
            else:
                print(f"  {model_id}: cached {field.n_times} steps")
        except Exception as exc:  # noqa: BLE001
            print(f"  {model_id}: FAILED ({exc})")

    print("Pre-seeding geo cache for Aegean + UK sample bboxes…")
    for label, start, finish in (
        ("aegean", AEGEAN_START, AEGEAN_FINISH),
        ("uk", (50.0, -5.0), (50.5, -4.5)),
    ):
        try:
            course = Course(
                start=start,
                finish=finish,
                marks=[],
                exclusions=[],
                depart_utc=now,
                min_depth_m=3.0,
            )
            ctx = ensure_geo_context(course, margin_deg=0.5)
            print(f"  {label}: land polygons={len(ctx.land.polygons)}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {label}: geo FAILED ({exc})")

    print("Done. The app can now route from cached GRIB fields.")


if __name__ == "__main__":
    main()
