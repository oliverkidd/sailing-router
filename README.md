# Sailing Weather Router

A **local-first** static route planner for offshore sailing races in **UK + Mediterranean** waters
(Aegean 600, Rolex Middle Sea, UK/Solent/RORC racing).

Given a configurable boat model, a hand-drawn race course, and one or more forecast models
(ECMWF, ICON, GFS via Open-Meteo, ARPEGE, UKMO, …), it computes a **wind-based isochrone
route per model**, overlays them on a MapLibre chart with LOD wind tiles, and shows a
per-model ETA / waypoint table (PredictWind-style comparison, no aggregation).

> **Region focus:** UK + Mediterranean courses. Forecast stack is **European-first** with **GFS**
> included for global compare via Open-Meteo (no direct NOAA infra).

> **Scope (v1):** static planner only — wind routing + land/depth masks. No currents,
> terrain-wind downscaling, live GPS/Signal K, accounts, or nav-grade charting.

## Architecture

- **Backend / router:** Python 3.11+ (FastAPI + NumPy). Forecast fetch, geo masks, and routing live here.
- **Frontend:** TypeScript + Next.js (React) + MapLibre GL JS. Requests routes and wind tiles as JSON.
- **Local-first:** prefetch wind + geo via `/course/prepare`, then `/route` can run against disk cache.

```
Browser (Next.js / MapLibre) ──REST/JSON──> FastAPI backend
        │                                          │
        └── OSM basemap tiles                      └──> local disk cache (wind · geo)
```

## Repo layout

```
sailing-router/
├─ apps/web/                 # Next.js + TypeScript frontend
├─ services/api/             # FastAPI backend (boat, polar, forecast, geo, metocean, router, cache)
├─ data/                     # sample-polars, sample-courses, masks, dem, fixtures
└─ docs/                     # build plan
```

## Getting started

```bash
# One command — installs deps on first run, then runs API + web together
npm run dev
```

Individual pieces are still available:

```bash
# Frontend only
npm run dev:web

# Backend only
cd services/api && source .venv/bin/activate && uvicorn app.main:app --reload

# Or both at once via make
make dev
```

### Offline fixtures

```bash
make fetch-fixtures   # cache Open-Meteo wind + geo for a sample Aegean bbox
```

## Local-first workflow

1. **With signal:** set course + models — the UI calls `/course/prepare` (wind + land/depth prefetch).
2. **Route:** `/route` computes one isochrone per model from cached (or freshly fetched) wind.

## Optional native dependencies

The default path (Open-Meteo wind + GSHHG land + EMODnet/GEBCO depth) needs **no native deps**.
Optional extras:

| Capability | Enable | Env |
| --- | --- | --- |
| Native GRIB (DWD/ECMWF/Météo-France) | `pip install -e ".[metocean]"` | `SAILING_FORECAST_NATIVE=1` |
| Copernicus DEM elevation | drop a GeoTIFF in `data/dem/` | — |

## API surface

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness |
| GET | `/models` | Full model catalog |
| GET | `/models/available` | Models covering a bbox |
| POST | `/boat` | Upload polar + boat config |
| GET | `/boat/sample` | Bundled sample boat |
| GET | `/boat/polar/{id}` | Polar curve for UI |
| GET | `/wind/grid` | LOD wind tile |
| POST | `/course/prepare` | Prefetch wind + geo |
| POST | `/route` | Isochrone routes |
| GET | `/elevation` | DEM / depth at a point |
