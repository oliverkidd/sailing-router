# Sailing Weather Router

A **local-first** static route planner for offshore sailing races in **UK + Mediterranean** waters
(Aegean 600, Rolex Middle Sea, UK/Solent/RORC racing).

Given a configurable boat model, a hand-drawn race course, and one or more free **European**
forecast models, it computes a **terrain- and current-aware isochrone route per model**, overlays
them in distinct colors on a terrain-and-depth-aware chart, and shows a per-model ETA / waypoint
table (PredictWind-style comparison, no aggregation).

> **Region lock:** UK + Mediterranean only. **No US systems, models, charts, or feeds**
> (no NOAA/NOMADS/GFS/HRRR, no NOAA ENC). Every connector is chosen for good UK + Med coverage
> and free access.

> **Scope (v1):** static planner only. No live GPS/Signal K, accounts, hosting, fleet/AIS, or
> nav-grade charting. See the build plan for the deferred v2+ backlog.

## Architecture

- **Backend / router:** Python 3.11+ (FastAPI + NumPy). All metocean / terrain / current / routing
  math lives here.
- **Frontend:** TypeScript + Next.js (React) + MapLibre GL JS. Never parses GRIB/NetCDF or runs
  WindNinja — it requests routes and sampled fields as JSON.
- **Native binary:** WindNinja (orographic wind downscaling), pinned via Docker.
- **Local-first:** fetch forecasts/currents ashore (or with signal), then route fully offline
  against the disk cache. FES tidal harmonics give currents with zero connectivity.

```
Browser (Next.js / MapLibre) ──REST/JSON──> FastAPI backend ──> WindNinja binary
        │                                          │
        └── static tiles (OSM, DEM-RGB, EMODnet)   └──> local disk cache
```

## Repo layout

```
sailing-router/
├─ apps/web/                 # Next.js + TypeScript frontend
├─ services/api/             # FastAPI backend (boat, polar, forecast, terrain_wind,
│                            #   currents, terrain, geo, metocean, router, cache)
├─ packages/shared-schemas/  # OpenAPI -> TS types
├─ data/                     # sample-polars, sample-courses, masks, dem, fixtures
└─ docs/                     # build plan
```

## Getting started

```bash
# Backend
cd services/api && python -m venv .venv && source .venv/bin/activate
pip install -e .
uvicorn app.main:app --reload

# Frontend
cd apps/web && pnpm install && pnpm dev

# Or both at once (from repo root)
make dev
```

### Offline fixtures

```bash
make fetch-fixtures   # small EU GRIB/JSON + DEM + depth + FES clips (tests never hit network)
```

## Local-first workflow

1. **Ashore / with signal:** pick models -> `/forecasts/fetch` + `/currents/fetch` (cache to disk).
2. **Offshore / offline:** draw/adjust course + boat config -> `/route` runs entirely against the
   cache. FES gives tides even with zero connectivity.

## Data sources (all European, free)

Forecast: Open-Meteo (default) / DWD / ECMWF open-data / Météo-France · Currents: Copernicus Marine
(MED + SMOC/NW-Shelf) + FES2014/2022 · Terrain: Copernicus DEM GLO-30 · Depth: EMODnet Bathymetry ·
Land/base: OSM / Natural Earth / OpenSeaMap · Orographic wind: WindNinja (OSS).

## License

MIT — see [LICENSE](LICENSE). Advisory only; **not** nav-grade. Always cross-check real navigation
software.
