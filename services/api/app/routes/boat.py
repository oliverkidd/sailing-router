"""/boat: upload a polar CSV + BoatConfig -> validated BoatConfig + polar_id.

Also exposes /boat/polar/{polar_id} so the UI can render a polar diagram, and
/boat/sample to bootstrap with the bundled sample polar.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .. import store
from ..boat import Boat
from ..polar import PolarTable
from ..schemas import BoatConfig, BoatResponse

router = APIRouter(tags=["boat"])


def _boat_response(cfg: BoatConfig, polar: PolarTable) -> BoatResponse:
    return BoatResponse(
        boat=cfg,
        polar_id=cfg.polar_id,
        tws_axis=[float(x) for x in polar.tws],
        twa_axis=[float(x) for x in polar.twa],
    )


@router.post("/boat", response_model=BoatResponse)
async def upload_boat(
    file: UploadFile = File(...),
    config: str = Form("{}"),
) -> BoatResponse:
    raw = await file.read()
    text = raw.decode("utf-8-sig")
    polar = None
    errors: list[str] = []
    for parser, label in ((PolarTable.from_csv, "matrix/CSV"), (PolarTable.from_pol, "pairwise .pol")):
        try:
            polar = parser(text)
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {exc}")
    if polar is None:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid polar file ({'; '.join(errors)})",
        ) from None

    polar_id = store.save_polar(file.filename or "polar", raw.decode("utf-8-sig"))
    cfg_dict = json.loads(config or "{}")
    cfg_dict["polar_id"] = polar_id
    cfg = BoatConfig(**cfg_dict)
    return _boat_response(cfg, polar)


@router.get("/boat/sample", response_model=BoatResponse)
def sample_boat() -> BoatResponse:
    polar_id = store.ensure_sample()
    polar = store.get_polar(polar_id)
    return _boat_response(BoatConfig(polar_id=polar_id), polar)


@router.get("/boat/polar/{polar_id}")
def get_polar_curve(polar_id: str) -> dict:
    """Return the polar grid + derived VMG target angles for a diagram."""
    try:
        polar = store.get_polar(polar_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    boat = Boat(BoatConfig(polar_id=polar_id), polar)
    targets = {
        float(t): {
            "upwind": boat.target_upwind_twa(float(t)),
            "downwind": boat.target_downwind_twa(float(t)),
        }
        for t in polar.tws
    }
    return {
        "polar_id": polar_id,
        "tws_axis": [float(x) for x in polar.tws],
        "twa_axis": [float(x) for x in polar.twa],
        "speed": [[float(v) for v in row] for row in polar.speed],
        "vmg_targets": targets,
    }
