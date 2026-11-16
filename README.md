<img src="assets/icon.svg" width="64" height="64" alt="">

# Agrobot — Sistema de Alertas Climáticas

Los productores configuran alertas sobre sus campos ("avisame si hay ≥ 70% de probabilidad de
helada en *Lote Norte*"). Un background job compara periódicamente los pronósticos almacenados con
los umbrales y notifica al usuario cuando se superan.

**Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.0 async (asyncpg) · PostgreSQL 16 · Alembic · pytest · uv · Docker.

> 🚧 En desarrollo. Avance por historia en [`docs/BACKLOG.md`](docs/BACKLOG.md).

## Cómo correrlo

### Todo con Docker
```bash
docker compose up --build          # db + migraciones + API (:8000) + worker
docker compose exec api python -m app.scripts.seed   # datos demo + pronósticos mock
```
API docs: http://localhost:8000/docs

### Local (desarrollo)
```bash
uv sync
make db-up        # Postgres en :5432 (crea agrobot y agrobot_test)
make migrate
make seed         # usuarios, campos, alertas y pronósticos mock
make api          # http://localhost:8000/docs
make worker       # job en loop  (o `make jobs-once` para un solo ciclo)
make check        # lint + typecheck + tests
```

## Arquitectura

```
API (FastAPI) ──▶ PostgreSQL ◀── Worker (asyncio)
                    ▲              1. Evaluador: INSERT…SELECT…ON CONFLICT DO NOTHING
  ingesta (mock) ───┘              2. Dispatcher: FOR UPDATE SKIP LOCKED → Notifier
```
Detalle en [`docs/JOBS.md`](docs/JOBS.md) y [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md).

## Documentación

| Doc | Contenido |
|-----|-----------|
| [REQUIREMENTS](docs/REQUIREMENTS.md) | Alcance, RF/RNF, **decisiones de producto** y supuestos |
| [BACKLOG](docs/BACKLOG.md) | Historias con criterios de aceptación |
| [DATA_MODEL](docs/DATA_MODEL.md) | Esquema, constraints e índices |
| [API](docs/API.md) | Endpoints y contratos |
| [JOBS](docs/JOBS.md) | Evaluador, dispatcher, worker, ingesta mock |
| [TESTING](docs/TESTING.md) | Estrategia y casos de test |
| [PROCESS](docs/PROCESS.md) | Proceso multi-agente (PM, coder, QA, reviewers) |

## Decisiones principales

Ver [`REQUIREMENTS.md` §6](docs/REQUIREMENTS.md#6-decisiones-de-producto-acordadas-con-el-usuario).
Esta sección se completa al cierre (S9) con trade-offs y próximos pasos.
