.PHONY: help dev dev-api dev-web install install-api install-web lint fetch-fixtures fetch-geo docker-build

help:
	@echo "Sailing Weather Router - make targets"
	@echo "  make install        Install backend (pip -e) + frontend (npm) deps"
	@echo "  make dev            Run API (uvicorn) + web (next dev) together"
	@echo "  make dev-api        Run FastAPI backend only"
	@echo "  make dev-web        Run Next.js frontend only"
	@echo "  make lint           Ruff lint backend"
	@echo "  make fetch-fixtures Prefetch Open-Meteo wind + geo for offline demo"
	@echo "  make fetch-geo      Download GSHHG coastlines + Natural Earth fallback"
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

lint:
	cd services/api && . .venv/bin/activate && ruff check app

fetch-fixtures:
	cd services/api && . .venv/bin/activate && python -m app.tools.fetch_fixtures

fetch-geo:
	cd services/api && . .venv/bin/activate && python scripts/fetch_gshhg.py

docker-build:
	docker compose build
