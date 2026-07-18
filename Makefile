.PHONY: help dev dev-api dev-web install install-api install-web lint fetch-fixtures fetch-geo fetch-geo-sample tiles tiles-sample wind-terrain wind-terrain-sample terrain-rgb build-data clean-tile-build docker-build

help:
	@echo "Sailing Weather Router - make targets"
	@echo "  make install        Install backend (pip -e) + frontend (npm) deps"
	@echo "  make dev            Run API (uvicorn) + web (next dev) together"
	@echo "  make dev-api        Run FastAPI backend only"
	@echo "  make dev-web        Run Next.js frontend only"
	@echo "  make build-data     Smart offline asset build (skips existing; npm run build-data)"
	@echo "  make lint           Ruff lint backend"
	@echo "  make fetch-fixtures Prefetch GRIB wind + geo for offline demo"
	@echo "  make fetch-geo-sample  Solent/IoW coastal Overpass (style preview)"
	@echo "  make tiles-sample   Build coastline.pmtiles for Solent sample only"
	@echo "  make fetch-geo      Full-region OSM land + coastal Overpass"
	@echo "  make tiles          Full-region coastline + EMODnet depth PMTiles"
	@echo "  make wind-terrain-sample  Identity/WindNinja terrain library (Solent)"
	@echo "  make wind-terrain   Region-wide terrain-response library bake"
	@echo "  make terrain-rgb    Cyclades MapLibre Terrain-RGB XYZ tiles"
	@echo "  make clean-tile-build  Delete build intermediates (keeps public/geo/*.pmtiles)"
	@echo "  make docker-build   Build the pinned eccodes + WindNinja image"

install: install-api install-web

install-api:
	cd services/api && python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"

install-web:
	cd apps/web && npm install

dev:
	@echo "Starting API on :8000 and web on :3000 ..."
	@$(MAKE) -j2 dev-api dev-web

dev-api:
	cd services/api && . .venv/bin/activate && uvicorn app.main:app --reload --port 8000

dev-web:
	cd apps/web && npm run dev

build-data:
	npm run build-data

lint:
	cd services/api && . .venv/bin/activate && ruff check app

fetch-fixtures:
	cd services/api && . .venv/bin/activate && python -m app.tools.fetch_fixtures

# Style preview: Isle of Wight / Solent only (fast Overpass + tippecanoe).
fetch-geo-sample:
	cd services/api && . .venv/bin/activate && \
		CHART_SAMPLE=1 python scripts/coastal_band.py --force --sample && \
		CHART_SAMPLE=1 python scripts/fetch_osm_chart_features.py --sample

tiles-sample: fetch-geo-sample
	@command -v tippecanoe >/dev/null || (echo "Install tippecanoe: brew install tippecanoe" && exit 1)
	cd services/api && . .venv/bin/activate && CHART_SAMPLE=1 python scripts/build_coastline_tiles.py --sample

# Full Europe chart region (lon -25..42, lat 30..72).
# Overpass is blocked/rate-limited for continent crawls — use Geofabrik PBFs.
fetch-geo:
	cd services/api && . .venv/bin/activate && python scripts/fetch_places.py && python scripts/fetch_osm_land.py && python scripts/coastal_band.py --force && python scripts/extract_osm_pbf_features.py

tiles: fetch-geo
	@command -v tippecanoe >/dev/null || (echo "Install tippecanoe: brew install tippecanoe" && exit 1)
	@command -v gdal_contour >/dev/null || (echo "Install gdal: brew install gdal" && exit 1)
	@command -v gdalbuildvrt >/dev/null || (echo "Install gdal: brew install gdal" && exit 1)
	@echo "Building chart tiles (OSM land + coastal places/roads/rivers/water + EMODnet depth). Expect long runtime."
	cd services/api && . .venv/bin/activate && python scripts/build_coastline_tiles.py
	cd services/api && . .venv/bin/activate && python scripts/build_depth_tiles.py

# Terrain-response library for 200 m WindNinja downscaling.
# Default is --identity (no WindNinja install). Pass WINDNINJA_BIN=... for a real bake.
wind-terrain-sample:
	cd services/api && . .venv/bin/activate && \
		python scripts/build_wind_terrain.py --sample --identity

wind-terrain:
	cd services/api && . .venv/bin/activate && \
		python scripts/build_wind_terrain.py $(if $(WINDNINJA_BIN),--windninja-bin $(WINDNINJA_BIN),--identity)

# MapLibre Terrain-RGB + hillshade source (Cyclades / CHART_CYCLADES bbox by default).
terrain-rgb:
	cd services/api && . .venv/bin/activate && \
		python scripts/build_terrain_rgb.py --cyclades

# Drop multi-GB Overpass / tippecanoe / EMODnet intermediates. Chart PMTiles on
# disk under apps/web/public/geo/ are kept; rebuild with `make tiles` if needed.
clean-tile-build:
	rm -rf data/cache/tiles_build data/cache/overpass
	@echo "Kept apps/web/public/geo/*.pmtiles (served from disk, not RAM)."
	@echo "Terrain-RGB under apps/web/public/geo/terrain/ is also kept."

docker-build:
	docker compose build
