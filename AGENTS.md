# AGENTS.md — Reglas para agentes de código

Leé esto entero antes de tocar código. Aplica a cualquier agente (coder, QA) que trabaje en el repo.

## Contexto en 30 segundos

API FastAPI + worker asyncio sobre PostgreSQL para alertas climáticas. Los usuarios configuran
alertas (campo + evento + umbral); un job compara los pronósticos guardados contra los umbrales y
genera notificaciones (outbox) que un dispatcher envía.

Documentos (fuente de verdad, en este orden):
1. `docs/REQUIREMENTS.md` — qué y por qué.
2. `docs/BACKLOG.md` — tu historia y sus criterios de aceptación.
3. `docs/DATA_MODEL.md`, `docs/API.md`, `docs/JOBS.md`, `docs/TESTING.md` — contratos exactos.

Si un documento y un stub se contradicen, **gana el documento**. Si dos documentos se contradicen o
algo no está especificado, **no inventes**: dejá la duda en tu reporte final (sección "Dudas").

## Setup

```bash
uv sync               # dependencias
make db-up            # Postgres en localhost:5432 (crea agrobot y agrobot_test)
make migrate          # aplica migraciones a la base de desarrollo
make check            # ruff + mypy + pytest (definition of done)
```

## Reglas duras

1. **Solo tocá los archivos de tu historia** (listados en `BACKLOG.md`). Si necesitás tocar otro,
   justificalo en el reporte.
2. **No cambies contratos**: nombres de tablas/columnas, paths HTTP, status codes, firmas de funciones
   públicas, nombres de fixtures. Están documentados y otras historias dependen de ellos.
3. **No modifiques** `migrations/env.py`, `app/core/*`, `app/models/base.py`, `app/models/enums.py`,
   `app/notifiers/base.py`, `app/main.py`, ni las fixtures existentes de `tests/conftest.py` (sí podés
   *agregar* factories ahí en S1). No agregues dependencias sin justificarlo.
4. **Todo async.** Nada de I/O bloqueante en el event loop (`time.sleep`, `requests`, drivers sync).
   Usá `AsyncSession` y `await` siempre.
5. **SQL:** usá SQLAlchemy 2.0 (`select()`, `insert()`, `postgresql.insert`). Nada de f-strings ni
   concatenación con SQL. `text()` solo con bind params.
6. **Capas:** router (HTTP, schemas) → service (reglas, queries, `commit`) → modelos. Los services
   lanzan `NotFoundError`/`ConflictError` (`app/core/errors.py`), **nunca** `HTTPException`.
   Excepción: validaciones de query params en el router (ej. `date_from > date_to` → 422).
7. **Ownership** en toda query de la API: filtrar siempre por el `user_id` del usuario autenticado.
   Si el recurso es de otro usuario, responder 404.
8. **Tipado estricto:** type hints completos; `mypy app` sin errores. Sin `# type: ignore` salvo con
   un comentario que explique por qué.
9. **Tests** para cada criterio de aceptación de la historia (ver `docs/TESTING.md`). Postgres real,
   sin mocks de la base. Fechas y horas fijas, nunca el reloj real.
10. **Logs:** `logger = logging.getLogger(__name__)`, formato `evento key=value`. Nunca loguear el
    teléfono completo.
11. Borrá los docstrings `TODO(Sx)` de los stubs que implementes y reemplazalos por docstrings
    reales y breves.
12. **No hagas commit, push ni PR**: eso lo hace el orquestador después de las reviews.
13. Si corregís un bug reportado por QA (`QA-n`), sacá el `@pytest.mark.xfail(... "QA-n" ...)` de su
    test: es `strict`, así que el test fallará como XPASS hasta que lo saques.

Tu rol específico (permisos, contexto y formato de entrega) está en `.agents/<rol>.md`.

## Estilo

- Python 3.12: `X | None`, `list[int]`, `StrEnum`, `datetime.UTC`.
- SQLAlchemy 2.0 tipado: `Mapped[int] = mapped_column(...)`.
- Pydantic v2: `model_config = ConfigDict(...)`, `Field(ge=, le=)`, `field_validator`/`model_validator`.
- Funciones chicas, nombres explícitos, sin código muerto ni prints.
- `make fmt` antes de terminar.

## Entrega

Respondé con el contrato de salida de tu rol (`.agents/coder.md` → `CODER_REPORT`,
`.agents/qa.md` → `QA_REPORT`).
