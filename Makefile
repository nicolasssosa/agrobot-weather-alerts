.PHONY: install db-up db-down migrate migration seed ingest api worker jobs-once test lint fmt typecheck check up down

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

worker:         ## Background job en loop
	uv run python -m app.jobs.worker

jobs-once:      ## Un solo ciclo de evaluación + despacho
	uv run python -m app.jobs.worker --once

test:           ## Requiere `make db-up`
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy app

check: lint typecheck test   ## Definition of done local

up:             ## Stack completo (db + migrate + api + worker)
	docker compose up --build

down:
	docker compose down -v
