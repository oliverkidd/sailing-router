# Sailing Weather Router

A **local-first** static route planner for offshore sailing races in **UK + Mediterranean** waters
(Aegean 600, Rolex Middle Sea, UK/Solent/RORC racing).

Given a configurable boat model, a hand-drawn race course, and one or more GRIB forecast models
(ECMWF IFS/AIFS, GFS, GEM, ICON-EU/D2, optional AROME/HARMONIE), it computes a **wind-based
isochrone route per model**, overlays them on a MapLibre chart from a single area wind series,
and shows a per-model ETA / waypoint table.

> **Forecast stack:** native **GRIB only** (no Open-Meteo). Free no-token sources work out of
> the box; Météo-France / KNMI models appear when their API keys are set.

> **Scope (v1):** static planner only — wind routing + land/depth masks. No currents,
> terrain-wind downscaling, live GPS/Signal K, accounts, or nav-grade charting.

## Architecture

- **Backend / router:** Python 3.11+ (FastAPI + NumPy + cfgrib/xarray). GRIB fetch → canonical
  `WindField` → Zarr store + SQLite catalog → `/wind/area` display series + routing.
- **Frontend:** TypeScript + Next.js (React) + MapLibre GL JS. Static UK+Europe PMTiles chart
  (coastline + depth isobaths) plus one `/wind/area` series for heatmap, arrows, scrubbing, and playback.
- **Local-first:** prefetch wind + geo via `/course/prepare`, then `/route` runs from disk cache.

```
Browser (Next.js / MapLibre) ──REST/JSON──> FastAPI backend
        │                                          │
        └── static PMTiles chart (public/geo/)     └──> Zarr wind + SQLite catalog + geo cache
            + wind heatmap/arrows
```

## Repo layout

```
sailing-router/
├─ apps/web/                 # Next.js + TypeScript frontend
├─ services/api/             # FastAPI backend (boat, polar, GRIB forecast, geo, router)
├─ data/                     # sample-polars, sample-courses, masks, dem, fixtures, cache
└─ docs/                     # build plan
```

## Getting started

```bash
# One command — installs deps on first run, then runs API + web together
npm run dev
```

### Backend GRIB dependencies

The API needs **cfgrib / eccodes / xarray / scipy / zarr / ecmwf-opendata** (declared in
`services/api/pyproject.toml`). Modern wheels usually bundle eccodes via `ecmwflibs`.

```bash
cd services/api
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
python -c "import cfgrib; print('cfgrib OK')"
```

If `import cfgrib` fails on macOS:

```bash
brew install eccodes
# then reinstall eccodes/cfgrib into the venv
pip install --force-reinstall eccodes cfgrib
```

### Optional API keys (free accounts)

Create `services/api/.env` (or export in your shell):

```bash
# Optional — enables AROME / ARPEGE-EU in the model picker
METEOFRANCE_API_KEY=your_key_here

# Optional — enables KNMI HARMONIE
KNMI_API_KEY=your_key_here
```

Without these keys, the models are simply omitted from `/models/available`.

### Nautical chart tiles (UK + Europe)

The map baseline is **static vector tiles** at `apps/web/public/geo/`
(`coastline.pmtiles`, `depth.pmtiles`) for the region `lon -25..42, lat 30..72`.
These files are **not in git** (they are hundreds of MB to >1 GB) — build them
locally once, then `npm run dev` serves them as static assets.

**Sources (no low-res fallbacks):** OpenStreetMap land polygons for coast/land;
EMODnet Bathymetry DTM for depth isobaths (2 m steps through 30 m, then 50/100/200 m).
Builds fail if either source is unavailable.

```bash
brew install tippecanoe gdal
cd services/api && source .venv/bin/activate && pip install rasterio numpy
cd ../.. && make tiles
```

For a deployed app, host the same PMTiles on object storage / a CDN and point
the map at that URL instead of committing them to the repo.

### Individual processes

```bash
# Frontend only
npm run dev:web

# Backend only
cd services/api && source .venv/bin/activate && uvicorn app.main:app --reload

# Or both via make
make dev
```

### Offline fixtures

```bash
make fetch-fixtures   # cache GRIB wind + geo for a sample Aegean bbox
```

## Local-first workflow

1. Zoom in below **300 mi** span so forecasts unlock.
2. Pick a model — the UI fetches `/wind/area` once for the viewport (cached by region).
3. Set course + models — `/course/prepare` prefetches native GRIB fields.
4. `/route` computes one isochrone per model; playback reuses the same area series.

## Models

| Model | Source | Coverage | Key |
| --- | --- | --- | --- |
| ECMWF IFS | ecmwf-opendata | Global | — |
| ECMWF AIFS | ecmwf-opendata | Global | — |
| GFS | NOAA AWS | Global | — |
| GEM GDPS | ECCC | Global | — |
| ICON-EU | DWD | Europe | — |
| ICON-D2 | DWD | Central Europe | — |
| AROME | Météo-France | France / W-Med | `METEOFRANCE_API_KEY` |
| ARPEGE-EU | Météo-France | Europe | `METEOFRANCE_API_KEY` |
| HARMONIE | KNMI | N. Sea / UK | `KNMI_API_KEY` |

## API surface

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness + configured models |
| GET | `/models` | Full model catalog |
| GET | `/models/available` | Models covering a bbox (key-gated) |
| POST | `/boat` | Upload polar + boat config |
| GET | `/boat/sample` | Bundled sample boat |
| GET | `/boat/polar/{id}` | Polar curve for UI |
| GET | `/wind/area` | Multi-hour display wind series |
| POST | `/course/prepare` | Prefetch wind + geo |
| POST | `/route` | Isochrone routes |
| GET | `/elevation` | DEM / depth at a point |

## Cache / migration

Wind fields live under `data/cache/forecast/*.zarr` with an index at
`data/cache/catalog.db`. After upgrading from the old Open-Meteo/npz tile cache, clear
stale files:

```bash
rm -rf data/cache/forecast/*.npz data/cache/forecast/*.zarr data/cache/catalog.db
```
