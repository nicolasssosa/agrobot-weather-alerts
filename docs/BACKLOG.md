# Backlog

Una historia = una branch `feat/<id>-<slug>` = un PR (squash merge a `main`).
Orden secuencial: cada historia parte de `main` con la anterior ya mergeada.
Specs de referencia: `DATA_MODEL.md`, `API.md`, `JOBS.md`, `TESTING.md`.

**DoD común a todas:** `make check` en verde · tests nuevos para cada criterio de aceptación ·
sin `TODO(Sx)` pendientes de la historia · contratos (firmas/paths/tablas) sin cambios no acordados ·
QA + Code review + Security review aprobados.

---

## S0 — Scaffolding, documentación y proceso · Owner: PM · ✅
Infra (pyproject, Docker, compose, Alembic async, config, DB, errores, enums, contratos de Notifier),
`conftest.py`, stubs y docs. Commit inicial en `main`.

## S1 — Modelo de datos y migración inicial · RNF-02, RNF-07 · ✅
Archivos: `app/models/{user,field,weather,alert,notification}.py`, `app/models/__init__.py`,
`migrations/versions/*_0001_initial_schema.py`, factories en `tests/conftest.py`, `tests/test_migrations.py`.
- [ ] Los 5 modelos coinciden con `DATA_MODEL.md` (tipos, nullability, CHECKs, UNIQUEs, índices parciales, `ondelete`).
- [ ] La migración `0001` hace upgrade y downgrade limpios (los corre `conftest`).
- [ ] `alembic check` sin diferencias (test).
- [ ] Factories `make_user/make_field/make_forecast/make_alert` + helper `auth()`.
- [ ] Tests de constraints: probability 101 → IntegrityError; alerta duplicada (campo, evento) → IntegrityError; notificación duplicada (alerta, día) → IntegrityError; borrar field cascadea a alerts/forecasts/notifications.

## S2 — Users, Fields y Health API · RF-01, RF-11, RNF-06, RNF-08
Archivos: `app/schemas/{common,user,field}.py`, `app/api/deps.py`, `app/services/{users,fields}.py`,
`app/api/routers/{health,users,fields}.py`, `tests/api/test_{health,users,fields}.py`.
- [ ] Endpoints de `API.md` §health, §users, §fields con sus códigos de estado.
- [ ] `get_current_user` → 401 si el header falta, es inválido o el usuario no existe.
- [ ] Ownership: el campo de otro usuario → 404.
- [ ] Paginación `Page[T]` con `limit`/`offset` validados.
- [ ] Todos los casos de `TESTING.md` para esos archivos.

## S3 — Datos meteorológicos: ingesta mock + consulta · RF-04, D3
Archivos: `app/weather/{provider,ingestion}.py`, `app/scripts/ingest.py`, `app/schemas/forecast.py`,
`app/services/forecasts.py`, `app/api/routers/forecasts.py`, `tests/weather/test_ingestion.py`, `tests/api/test_forecasts.py`.
- [ ] `MockWeatherProvider` determinístico: `days × 5` puntos por campo, 0..100.
- [ ] `ingest_forecasts` hace upsert (re-ejecutar actualiza, no duplica) y pagina los campos.
- [ ] `python -m app.scripts.ingest --days 14` funciona contra la DB local.
- [ ] `GET /fields/{id}/forecasts` con filtros, orden y validación `date_from <= date_to`.

## S4 — Alerts API · RF-02, RF-03, RF-11, D4, D5
Archivos: `app/schemas/alert.py`, `app/services/alerts.py`, `app/api/routers/alerts.py`, `tests/api/test_alerts.py`.
- [ ] CRUD completo según `API.md` §alerts.
- [ ] Duplicado (campo, evento) → 409, detectado con IntegrityError (no SELECT previo).
- [ ] PATCH parcial; `{}` → 422; `field_id`/`event_type` no editables (422 por `extra="forbid"`).
- [ ] DELETE cascadea a las notificaciones.

## S5 — Evaluador de alertas · RF-05, RF-06, RF-08, RNF-03, D1, D2
Archivos: `app/jobs/evaluator.py`, `tests/jobs/test_evaluator.py`.
- [ ] Un único `INSERT ... SELECT ... ON CONFLICT DO NOTHING RETURNING` (nada de loops en Python).
- [ ] Todos los casos de `TESTING.md` §test_evaluator (borde `==`, inactiva, pasado, hoy, futuro lejano, otro evento/campo, idempotencia).

## S6 — Dispatcher, Notifier y API de notificaciones · RF-07, RF-09, RF-10, RNF-03, RNF-04, D7, D10
Archivos: `app/jobs/dispatcher.py`, `app/notifiers/log.py`, `app/schemas/notification.py`,
`app/services/notifications.py`, `app/api/routers/notifications.py`, `tests/jobs/test_dispatcher.py`, `tests/api/test_notifications.py`.
- [ ] `FOR UPDATE OF notifications SKIP LOCKED`, lote limitado, timeout por envío.
- [ ] Éxito/reintento/backoff/`failed` exactamente como `JOBS.md`.
- [ ] Texto del mensaje exacto. Teléfono enmascarado en el log.
- [ ] `GET /notifications` con ownership y filtros.
- [ ] (Opcional) test de concurrencia con dos conexiones.

## S7 — Worker y seed · RF-05, RNF-04, RNF-09
Archivos: `app/jobs/worker.py`, `app/scripts/seed.py`, `tests/jobs/test_worker.py`.
- [ ] `run_cycle` (evaluar + commit, despachar en lotes + commit), `run_forever` resiliente e interrumpible, `main --once`.
- [ ] SIGTERM → shutdown limpio (termina el ciclo en curso).
- [ ] Seed idempotente según `JOBS.md` §Seed.
- [ ] Prueba manual documentada en el PR: `make db-up migrate seed jobs-once` → notificaciones `sent`.

## S8 — CI (GitHub Actions) · RNF-07
Archivos: `.github/workflows/ci.yml`.
- [ ] Trigger en `pull_request` y `push` a `main`.
- [ ] Job con service `postgres:16-alpine` (crea `agrobot_test`), `astral-sh/setup-uv`, `uv sync --frozen`, `ruff check`, `ruff format --check`, `mypy app`, `pytest`.
- [ ] Job `docker build` (sin push) para validar el Dockerfile.
- [ ] Permisos mínimos (`permissions: contents: read`), acciones pinneadas por versión mayor, cache de uv.

## S9 — README final y cierre · Owner: PM
- [ ] README: cómo correr (compose y local), arquitectura, decisiones (de `REQUIREMENTS.md` §6), trade-offs, qué faltó / próximos pasos.
- [ ] Verificación end-to-end con `docker compose up --build`.

---

## Follow-ups (hallazgos minor/nit de las reviews)

| Origen | Severidad | Descripción | Destino sugerido |
|--------|-----------|-------------|------------------|
| S1 SEC-2 | minor | CHECK E.164 en `users.phone_number` en la DB (hoy solo lo valida Pydantic; variantes del número esquivan el UNIQUE) | migración 0002 junto con S2 |
| S1 SEC-1 | minor | Tope de 1000 caracteres en `notifications.last_error` a nivel DB + enmascarado de PII en el error | migración 0002 / S6 |
| S1 CR-2 | nit | Derivar la lista de valores del CHECK de `event_type` de `EventType` en vez de repetirla | próxima migración |
| S1 CR-1 | nit | Migración: nombres de PK/UQ/FK explícitos vs. CHECK en forma base (inconsistencia sin impacto) | — |
