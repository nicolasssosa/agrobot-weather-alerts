.PHONY: demo install db-up db-down migrate migration seed ingest api worker jobs-once openapi test cov lint fmt typecheck check up down

install:        ## Instala dependencias (incluye dev)
	uv sync

db-up:          ## Levanta solo Postgres (puerto 5432)
	docker compose up -d --wait db

db-down:
	docker compose down

migrate:        ## Aplica migraciones a DATABASE_URL
	uv run alembic upgrade head

migration:      ## Crea una migración vacía: make migration m="mensaje"
	uv run alembic revision -m "$(m)"

seed:           ## Crea usuarios/campos/alertas demo + forecasts mockeados
	uv run python -m app.scripts.seed

ingest:         ## Corre una ingesta mock de forecasts (simula el job existente)
	uv run python -m app.scripts.ingest

api:            ## API con autoreload en http://localhost:8000/docs
	uv run uvicorn app.main:app --reload

openapi:        ## Regenera docs/openapi.json (contrato OpenAPI versionado)
	uv run python -m app.scripts.export_openapi

worker:         ## Background job en loop
	uv run python -m app.jobs.worker

jobs-once:      ## Un solo ciclo de evaluación + despacho
	uv run python -m app.jobs.worker --once

test:           ## Requiere `make db-up`
	uv run pytest

cov:            ## Tests con cobertura: resumen en terminal + HTML en htmlcov/
	uv run pytest --cov=app --cov-report=term --cov-report=html

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy app

check: lint typecheck test   ## Definition of done local

API_URL ?= http://localhost:8000

demo:           ## Demo en un comando: stack en Docker + seed + un ciclo del job + notificaciones enviadas
	docker compose up --build -d --wait api worker
	docker compose exec -T api python -m app.scripts.seed
	docker compose exec -T worker python -m app.jobs.worker --once
	@echo "--- Notificaciones enviadas a Ana (X-User-Id: 1):"
	@curl -sf "$(API_URL)/api/v1/notifications?status=sent&limit=5" -H "X-User-Id: 1"
	@echo
	@echo "--- Swagger UI: $(API_URL)/docs · logs del worker: docker compose logs -f worker"

up:             ## Stack completo (db + migrate + api + worker)
	docker compose up --build

down:
	docker compose down -v
