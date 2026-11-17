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

## S2 — Users, Fields y Health API + hardening de esquema · RF-01, RF-11, RNF-06, RNF-08 · ✅
Archivos: `app/schemas/{common,user,field}.py`, `app/api/deps.py`, `app/services/{users,fields}.py`,
`app/api/routers/{health,users,fields}.py`, `tests/api/test_{health,users,fields}.py`,
`migrations/versions/*_0002_constraints_hardening.py`, `app/models/{user,notification}.py`
(solo los CheckConstraint nuevos), `tests/test_models.py`, `tests/test_schema.py` (solo agregar casos de 0002).
- [ ] Migración `0002` según `DATA_MODEL.md` §Migración 0002 (follow-ups S1 SEC-1 y SEC-2): CHECK E.164 en `users.phone_number` y largo máximo de `notifications.last_error`; upgrade/downgrade limpios; `alembic check` sin diferencias; tests de ambos CHECK (válido / inválido / borde 1000 vs 1001).
- [ ] Endpoints de `API.md` §health, §users, §fields con sus códigos de estado.
- [ ] `get_current_user` → 401 si el header falta, es inválido o el usuario no existe.
- [ ] Ownership: el campo de otro usuario → 404.
- [ ] Paginación `Page[T]` con `limit`/`offset` validados.
- [ ] Todos los casos de `TESTING.md` para esos archivos.

## S3 — Datos meteorológicos: ingesta mock + consulta · RF-04, D3 · ✅
Archivos: `app/weather/{provider,ingestion}.py`, `app/scripts/ingest.py`, `app/schemas/forecast.py`,
`app/services/forecasts.py`, `app/api/routers/forecasts.py`, `tests/weather/test_ingestion.py`, `tests/api/test_forecasts.py`.
- [ ] `MockWeatherProvider` determinístico: `days × 5` puntos por campo, 0..100.
- [ ] `ingest_forecasts` hace upsert (re-ejecutar actualiza, no duplica) y pagina los campos.
- [ ] `python -m app.scripts.ingest --days 14` funciona contra la DB local.
- [ ] `GET /fields/{id}/forecasts` con filtros, orden y validación `date_from <= date_to`.

## S4 — Alerts API · RF-02, RF-03, RF-11, D4, D5 · ✅
Archivos: `app/schemas/alert.py`, `app/services/alerts.py`, `app/api/routers/alerts.py`, `tests/api/test_alerts.py`,
`app/core/errors.py` (solo el handler de SEC-4), `app/services/_db_errors.py` (nuevo), `app/services/{users,fields}.py` (solo reemplazar el helper duplicado), tests de 422 en `tests/api/`.
- [ ] CRUD completo según `API.md` §alerts.
- [ ] Duplicado (campo, evento) → 409, detectado con IntegrityError (no SELECT previo).
- [ ] PATCH parcial; `{}` → 422; `field_id`/`event_type` no editables (422 por `extra="forbid"`).
- [ ] DELETE cascadea a las notificaciones.
- [ ] Follow-up S2 SEC-4 (autorizado tocar `app/core/errors.py`): handler global de `RequestValidationError` que sanee `input` de los errores (surrogates UTF-16, NaN/Infinity) para que todo 422 sea JSON serializable; tests con surrogate suelto en `name` de users, fields y un payload de alerts.
- [ ] Follow-up S2 CR-dup: mover `_is_unique_violation` a un único módulo compartido (ej. `app/services/_db_errors.py`) y usarlo en users, fields y alerts.

## S5 — Evaluador de alertas · RF-05, RF-06, RF-08, RNF-03, D1, D2 · ✅
Archivos: `app/jobs/evaluator.py`, `tests/jobs/test_evaluator.py`.
- [ ] Un único `INSERT ... SELECT ... ON CONFLICT DO NOTHING RETURNING` (nada de loops en Python).
- [ ] Todos los casos de `TESTING.md` §test_evaluator (borde `==`, inactiva, pasado, hoy, futuro lejano, otro evento/campo, idempotencia).

## S6 — Dispatcher, Notifier y API de notificaciones · RF-07, RF-09, RF-10, RNF-03, RNF-04, D7, D10 · ✅
Archivos: `app/jobs/dispatcher.py`, `app/notifiers/log.py`, `app/schemas/notification.py`,
`app/services/notifications.py`, `app/api/routers/notifications.py`, `tests/jobs/test_dispatcher.py`, `tests/api/test_notifications.py`.
- [ ] Patrón lease (JOBS.md §Dispatcher): claim corto con `FOR UPDATE OF notifications SKIP LOCKED` + lease vía `next_attempt_at`, envío fuera de transacción con concurrencia acotada, resultados en otra transacción corta. Timeout por envío.
- [ ] Éxito/reintento/backoff/`failed` exactamente como `JOBS.md`.
- [ ] Texto del mensaje exacto. Teléfono enmascarado en el log.
- [ ] `GET /notifications` con ownership y filtros.
- [ ] (Opcional) test de concurrencia con dos conexiones.

## S7 — Worker y seed · RF-05, RNF-04, RNF-09 · ✅
Archivos: `app/jobs/worker.py`, `app/scripts/seed.py`, `tests/jobs/test_worker.py`.
- [ ] `run_cycle` (evaluar + commit, despachar en lotes + commit), `run_forever` resiliente e interrumpible, `main --once`.
- [ ] SIGTERM → shutdown limpio (termina el ciclo en curso).
- [ ] Seed idempotente según `JOBS.md` §Seed.
- [ ] Prueba manual documentada en el PR: `make db-up migrate seed jobs-once` → notificaciones `sent`.

## S8 — OpenAPI / Swagger como contrato · RNF-07 · ✅
Archivos: `app/main.py` (solo metadata OpenAPI: descripción y `openapi_tags`), `app/api/deps.py`
(esquema de seguridad), `app/schemas/*.py` (ejemplos + `ErrorResponse` en `common.py`),
`app/api/routers/*.py` (`responses=` y summaries), `app/scripts/export_openapi.py` (nuevo),
`docs/openapi.json` (generado), `Makefile` (target `openapi`), `tests/test_openapi_contract.py` (nuevo).
- [ ] Schema común `ErrorResponse {detail: str}`; cada endpoint declara en `responses=` los códigos que realmente devuelve (401 sin/mal header, 404 por ownership/inexistente, 409 por duplicado, 422 validación) según `API.md`.
- [ ] `X-User-Id` como esquema de seguridad `apiKey` en header (`APIKeyHeader(name="X-User-Id", auto_error=False)`): botón **Authorize** en `/docs`; el comportamiento no cambia (401 con el mismo mensaje). Endpoints públicos (`POST /users`, `/health`) sin seguridad.
- [ ] Ejemplos válidos en todos los requests y responses (`json_schema_extra` / `Field(examples=...)`): "Try it out" funciona sin editar el body.
- [ ] Descripción de la API (cómo autenticarse, flujo típico: crear usuario → campo → alerta → ver notificaciones) y descripción por tag.
- [ ] `python -m app.scripts.export_openapi` / `make openapi` escribe `docs/openapi.json` (JSON ordenado, determinista).
- [ ] Test de contrato: `app.openapi()` == `docs/openapi.json`; si difieren, falla con el mensaje "corré `make openapi`". Tests de que cada endpoint protegido declara 401 y el security scheme, y que los ejemplos validan contra sus propios schemas.
- [ ] Ningún cambio de comportamiento: toda la suite existente sigue verde sin modificarse.

## S9 — Alertas: borrado lógico, varios umbrales y validación al enviar · D5, D11, D12 · ✅
Archivos: migración `0003`, `app/models/{alert,enums}.py`, `app/schemas/alert.py`,
`app/services/alerts.py`, `app/api/routers/alerts.py`, `app/jobs/{evaluator,dispatcher}.py`,
`docs/openapi.json`, tests.
- [ ] `alerts.deleted_at`; DELETE lógico e idempotente (204 también si ya estaba borrada; 404 si no existe o es ajena); las alertas borradas no aparecen en GET ni se evalúan.
- [ ] Al borrar, las notificaciones `pending` de esa alerta pasan a `cancelled` (motivo `alert_deleted`); el historial (`sent`/`failed`/`cancelled`) sigue visible en `GET /notifications`.
- [ ] Unicidad `(field_id, event_type, threshold)` solo entre alertas no borradas → 409; se puede recrear una alerta borrada.
- [ ] Estado `cancelled` en notificaciones. El dispatcher, en el claim, cancela (sin enviar) las que ya no corresponden: alerta borrada, inactiva o probabilidad < umbral actual.
- [ ] OpenAPI regenerado y contrato actualizado.

## S10 — Idempotency-Key en los POST · D13 · ✅
- [ ] Header opcional `Idempotency-Key` en `POST /users`, `/fields`, `/alerts`: replay de la respuesta original, 422 si el body cambió, vigencia 24 h, seguro ante requests concurrentes con la misma clave. Documentado en OpenAPI.

## S11 — Calidad de pronósticos · D14 · ✅
- [ ] `weather_forecasts.issued_at`; upsert condicional (solo datos más nuevos); evaluador ignora pronósticos desactualizados y loguea un warning con la cantidad; `issued_at` visible en `GET /forecasts`.

## S11b — Idempotency-Key obligatoria solo en `POST /alerts` · D13 · ✅
- [ ] `POST /alerts` sin `Idempotency-Key` → 422 con el mensaje de API.md; el resto del comportamiento de la clave no cambia.
- [ ] `POST /users` y `POST /fields` sin soporte de `Idempotency-Key` (el header se ignora) y sin el código especial de `/users` (scope por body, reconstrucción sin PII).
- [ ] Migración 0006 según DATA_MODEL.md; OpenAPI con el header **requerido** en `POST /alerts` y sin header en los otros dos.

## S12a — Zona horaria por campo y horizonte configurable · D2, D9 · ✅
- [x] `fields.timezone` (IANA, validada; default `America/Argentina/Buenos_Aires`), en `FieldCreate` (opcional) y `FieldRead`; migración 0007.
- [x] El evaluador calcula "hoy" **por campo** en su zona horaria y evalúa `[hoy_local, hoy_local + ALERT_HORIZON_DAYS)` (0 = sin límite).
- [x] Follow-ups de S11: los scripts `ingest`/`seed` usan `FORECAST_FUTURE_TOLERANCE_MINUTES` del setting (SEC-3); warning `future_forecasts count=<n> fields=<m>` cuando el evaluador excluye pronósticos con `issued_at` adelantado (SEC-4); la validación al enviar compara contra la probabilidad del **pronóstico vigente** (SEC-5).

## S12c — Límites de paginación e ids en OpenAPI · ✅
Archivos: `app/schemas/common.py`, `app/api/routers/alerts.py`, `app/api/routers/notifications.py`, `docs/openapi.json`, tests.
- [x] `offset` acotado a 0..10000 (`offset=10001` → 422).
- [x] Los filtros `field_id`/`alert_id` siguen rechazando valores fuera de BIGINT con 422, sin publicar `maximum` en el schema OpenAPI (ningún parámetro de la API publica un `maximum` mayor a 10000).

## S12b — Re-aviso por episodio · RF-08, D1 · ✅
Archivos: `migrations/versions/20261117_0008_alert_episodes.py` (nuevo), `app/models/alert_day_state.py`
(nuevo), `app/models/__init__.py`, `app/models/notification.py`, `app/jobs/episodes.py` (nuevo),
`app/jobs/evaluator.py`, `app/jobs/worker.py`, `app/core/config.py` (solo `rearm_margin`),
`.env.example`, `app/services/alerts.py`, `app/schemas/notification.py`, `docs/openapi.json`, tests.
Contrato: DATA_MODEL.md §alert_day_states y §Migración 0008, JOBS.md §Evaluador y §Episodios,
API.md §PATCH alerts y §notifications.
- [x] Migración 0008 (tabla `alert_day_states`, `notifications.episode`, UNIQUE nuevo, backfill, downgrade que falla con episodios > 1); `alembic check` limpio.
- [x] `app/jobs/episodes.py`: `DayState`, `INITIAL_STATE`, `rearm_level`, `step`, `reset` exactos según JOBS.md, con tests unitarios de cada fila de la tabla de transición y del borde de `rearm_level` (incluido el mínimo 1).
- [x] Evaluador por episodio: lock de evaluación (`pg_advisory_xact_lock`), re-armado y disparo set-based según JOBS.md; log `alerts_evaluated created=<n> rearmed=<m>`. Se mantienen ventana local, horizonte, frescura, zonas inválidas y los warnings de S11/S12a.
- [x] Re-aviso en cada cruce hacia arriba (episodio 2, 3…); sin re-aviso mientras sigue arriba ni dentro de la banda de histéresis; re-ejecutar el ciclo no duplica.
- [x] `REARM_MARGIN` (`Settings.rearm_margin`, default 10, 0..99) en config, `.env.example` y pasado por el worker.
- [x] PATCH que cambia el umbral o **reactiva** (`is_active` false → true; QA-1 de S9) re-arma y cancela las `pending` no arrendadas con `alert_reset`; el siguiente ciclo re-avisa. Un PATCH sin esos cambios no re-arma ni cancela.
- [x] Tests de equivalencia: secuencias determinísticas (semilla fija) de probabilidades por ciclo, con cambios de umbral y pausa/reactivación intercalados, corridas con el evaluador SQL y con `step`/`reset`: mismas notificaciones (día, episodio) y mismo estado final.
- [x] Test de concurrencia: dos evaluaciones en conexiones separadas sobre los mismos datos no generan episodios duplicados.
- [x] `NotificationRead.episode` en la API y en `docs/openapi.json`.

## S13 — Envío agrupado por usuario + horario de silencio · RF-07, RNF-03, D15
Archivos: `migrations/versions/20261117_0009_user_quiet_hours.py` (nuevo), `app/models/user.py`,
`app/schemas/user.py`, `app/services/users.py`, `app/api/routers/users.py`,
`app/jobs/quiet_hours.py` (nuevo), `app/jobs/dispatcher.py`, `app/jobs/worker.py`,
`app/core/config.py` (solo `digest_max_items`), `.env.example`, `docs/openapi.json`, tests.
Contrato: DATA_MODEL.md §users y §Migración 0009, API.md §users, JOBS.md §Envío agrupado por
usuario y horario de silencio y §Worker.
- [ ] Migración 0009 (`users.timezone`, `quiet_hours_start/end`, CHECKs) con downgrade; modelo igual; `alembic check` limpio.
- [ ] `PATCH /users/me` (timezone y quiet_hours; `{}` → 422; `null` desactiva; omitir no cambia; `start == end`, segundos o zona en la hora → 422; zona desconocida → 422); `POST /users` acepta ambos opcionales; `UserRead` con `timezone` y `quiet_hours` (`"HH:MM"` o `null`).
- [ ] `app/jobs/quiet_hours.py`: `quiet_until` exacto según JOBS.md, con tests unitarios: sin config, ventana del mismo día (adentro, afuera, borde `start` incluido, borde `end` excluido), ventana que cruza la medianoche (antes y después de las 00:00), conversión desde UTC con una zona no-UTC, `now` sin zona → `ValueError`.
- [ ] Dispatcher: un mensaje por usuario y lote (claim por usuario con `FOR NO KEY UPDATE SKIP LOCKED`), tope `digest_max_items` por mensaje (el excedente sale en el lote siguiente), `notification_id` = menor id, 1 notificación → texto de siempre, ≥ 2 → `build_digest_text` exacto.
- [ ] Resultados por notificación: un envío fallido aplica reintento/`failed` a cada una según sus propios `attempts`; la guarda de lease se mantiene por fila.
- [ ] Horario de silencio: usuario en silencio → sus notificaciones válidas quedan `pending` con `next_attempt_at` = fin del silencio, sin tocar `attempts`, contadas en `deferred`, sin enviar; al llegar la hora salen en un solo mensaje. Las que ya no corresponden se cancelan igual durante el silencio.
- [ ] Cancelación nueva `forecast_date_passed` (día pronosticado anterior al "hoy" del campo), en el orden de JOBS.md.
- [ ] `DispatchResult.deferred` y `.users`; log `dispatch_done` con ambos; el worker sigue drenando mientras `users == batch_size`.
- [ ] `DIGEST_MAX_ITEMS` (`Settings.digest_max_items`, default 10, rango 1..20) en config, `.env.example` y pasado por el worker.
- [ ] Concurrencia: dos `dispatch_pending` en conexiones separadas sobre el mismo usuario con varias notificaciones → un solo mensaje para ese usuario y ninguna notificación enviada dos veces.
- [ ] Los tests existentes del dispatcher y el worker siguen verdes (se ajustan solo los que asumían un mensaje por notificación para un mismo usuario; justificarlo en el reporte).
- [ ] OpenAPI regenerado (`make openapi`) y test de contrato verde.

## S14 — Operación · D16
- [ ] `statement_timeout`/`lock_timeout`; cuotas por usuario; notificaciones `failed` reintentables; job de retención (pronósticos pasados, `alert_day_states` de días pasados, claves de idempotencia vencidas); límite de tamaño de request y de cantidad de errores en el 422 (follow-ups S4).

## S15 — Observabilidad
- [ ] Logs JSON estructurados con `request_id`; métricas Prometheus (`/metrics`); `GET /health/live` y `/health/ready` (DB + heartbeat del worker).

## S16 — CI (GitHub Actions) + pre-commit + cobertura + GitHub Pages · RNF-07
Archivos: `.github/workflows/ci.yml` (nuevo), `site/index.html` (nuevo), `.pre-commit-config.yaml` (nuevo),
`pyproject.toml` + `uv.lock` (dependencia dev `pytest-cov` y config de cobertura), `Makefile` (target `cov`),
`.gitignore` (salidas de cobertura), `app/jobs/worker.py` (SEC-3), `app/scripts/seed.py` (SEC-5),
`docker-compose.yml` (SEC-5), tests.

CI:
- [ ] Trigger en `pull_request` y `push` a `main`; `concurrency` por ref que cancela corridas viejas del mismo PR.
- [ ] Job `test`: service `postgres:16-alpine` con healthcheck, crea `agrobot_test`; `astral-sh/setup-uv` con cache de uv; `uv sync --frozen`; `ruff check`, `ruff format --check`, `mypy app`, `pytest` con cobertura (`--cov=app`, reporte en terminal, XML y HTML). `TEST_DATABASE_URL` apunta al service.
- [ ] Cobertura con umbral mínimo en `pyproject.toml` (`[tool.coverage.report] fail_under`): el valor medido en esta historia redondeado hacia abajo al múltiplo de 5. El HTML se sube como artifact del workflow.
- [ ] El test de contrato OpenAPI de S8 corre como paso propio y visible (`pytest tests/test_openapi_contract.py`) antes de la suite completa.
- [ ] Job `docker`: `docker build` (sin push) para validar el Dockerfile.
- [ ] Permisos mínimos: `permissions: contents: read` a nivel workflow; solo el job de deploy agrega `pages: write` e `id-token: write`. Acciones pinneadas por versión mayor. Sin secretos.

GitHub Pages (solo en `push` a `main`, después de que `test` y `docker` pasen):
- [ ] Job `pages-build`: arma el sitio en `_site/` con `site/index.html` (Swagger UI estático que carga `openapi.json`), `docs/openapi.json` copiado a `_site/openapi.json` y el reporte HTML de cobertura en `_site/coverage/`; lo sube con `actions/upload-pages-artifact`.
- [ ] Job `pages-deploy`: `actions/deploy-pages`, `environment: github-pages`, `concurrency: pages` sin cancelar un deploy en curso.
- [ ] `site/index.html`: Swagger UI desde un CDN con versión exacta pinneada (`swagger-ui-dist@<x.y.z>`), sin "Try it out" (no hay backend público: `supportedSubmitMethods: []`), link visible al reporte de cobertura. Funciona servido desde un subpath (`/<repo>/`): rutas relativas.

Pre-commit:
- [ ] `.pre-commit-config.yaml` con los hooks de ruff (check con `--fix` y format) pinneados por tag; `pre-commit` no se agrega como dependencia del proyecto (se instala aparte, documentado en el propio archivo).

Follow-ups:
- [ ] S7 SEC-3: `worker --once` le pasa `stop_event` a `run_cycle` (+ test: con el evento seteado no arranca un lote nuevo).
- [ ] S7 SEC-5: el seed exige `ENVIRONMENT` definida explícitamente (variable de entorno o `.env`; se detecta con `model_fields_set` de los settings) o el flag `--yes-demo`; si no, aborta con mensaje claro y exit code ≠ 0. `ENVIRONMENT: local` declarado en el `app-env` del compose. Tests de los tres casos.

## S17 — README final, `make demo`, Postman y review integral · Owner: PM
- [ ] README: cómo correr (compose y local), arquitectura, decisiones (de `REQUIREMENTS.md` §6), trade-offs, qué faltó / próximos pasos.
- [ ] Verificación end-to-end con `docker compose up --build`.

---

## Follow-ups (hallazgos minor/nit de las reviews)

| Origen | Severidad | Descripción | Destino sugerido |
|--------|-----------|-------------|------------------|
| S1 SEC-2 | minor | CHECK E.164 en `users.phone_number` en la DB (hoy solo lo valida Pydantic; variantes del número esquivan el UNIQUE) | ✅ resuelto en S2 (migración 0002) |
| S1 SEC-1 | minor | Tope de 1000 caracteres en `notifications.last_error` a nivel DB + enmascarado de PII en el error | ✅ CHECK en S2 (0002); enmascarado de PII en S6 (`app/core/pii.py`) |
| S1 CR-2 | nit | Derivar la lista de valores del CHECK de `event_type` de `EventType` en vez de repetirla | próxima migración |
| S1 CR-1 | nit | Migración: nombres de PK/UQ/FK explícitos vs. CHECK en forma base (inconsistencia sin impacto) | — |
| S2 SEC-4 | minor | Surrogate UTF-16 suelto en un string → el 422 de FastAPI no serializa → 500. Fix: handler global de `RequestValidationError` que sanee `input` (cubre también NaN/surrogates en schemas futuros) | ✅ resuelto en S4 |
| S2 SEC-3 | minor | Enumeración de teléfonos vía 409 en `POST /users` público. Mitigación: rate limit por IP + log enmascarado; eliminar signup público con auth real | README §Próximos pasos |
| S2 CR-3 | minor | `X-User-Id` con > 19 caracteres (ceros a la izquierda) → 401 aunque el valor sea válido (falla cerrada) | ✅ documentado en API.md §Convenciones |
| S2 CR-dup | nit | `_is_unique_violation` duplicado en services users/fields | ✅ resuelto en S4 |
| S3 SEC-note | minor | Sin retención de `weather_forecasts` (crece ~1.825 filas/campo/año) ni tope de rango en `GET /forecasts`. Mitigación: job de purga de fechas pasadas y/o máximo `date_to - date_from` (ej. 366 días) | README §Próximos pasos |
| S4 CR-4 + SEC-2 | minor | Sin límite de tamaño de body ni de cantidad de errores en el 422 (20k claves extra → 422 de 2 MB). Fix: middleware de tamaño máximo de request (ej. 64 KB) + tope de errores en el handler | README §Próximos pasos |
| S5 SEC-1 | minor | Sin cuota de campos/alertas por usuario: un usuario puede inflar una corrida del evaluador (y luego el envío). Mitigación: `MAX_FIELDS_PER_USER` en el service de fields + rate limit; opcional horizonte o tope por ciclo | README §Próximos pasos |
| S5 CR-3 | minor | Un DELETE de alerta que commitea durante el INSERT…SELECT puede abortar el statement por FK (23503); el worker reintenta al próximo ciclo (autocorrige) | README §Limitaciones |
| S5 CR-4 | nit | Carrera con PATCH `is_active=false`: una corrida en curso puede notificar una alerta recién pausada (aislamiento por snapshot, at-least-once) | README §Limitaciones |
| S6 SEC-5 | minor | Enmascarado de `last_error` optimizado para Argentina: formatos nacionales de otros países y separadores Unicode raros pueden quedar en claro. Fix: fallback que enmascare toda secuencia de ≥ 8 dígitos | README §Próximos pasos |
| S7 SEC-3 | minor | `worker --once` no le pasa `stop_event` a `run_cycle`: un SIGTERM espera a drenar toda la cola (riesgo de SIGKILL y reenvío al vencer el lease). Fix de una línea + test | **S16** |
| S7 SEC-4 | minor | El seed reutiliza un usuario existente con el teléfono demo (aunque sea real) y le agrega datos mock. Fix: abortar si el nombre no coincide o usar un rango de teléfonos no asignable | README §Limitaciones |
| S7 SEC-5 | minor | La guarda del seed es denylist y `environment` default es `local`: si `ENVIRONMENT` no está seteada, pasa. Fix: exigir `ENVIRONMENT` explícita o flag `--yes-demo`; declarar `ENVIRONMENT` en el compose | **S16** |
| S7 CR-1 | minor | Seed no seguro ante dos corridas concurrentes (documentado en el docstring) | — |
| S9 CR-2 | nit | Migración 0003 transaccional (bloquea escrituras mientras crea el índice); en producción con tablas grandes usar `CONCURRENTLY` / `NOT VALID` (documentado en DATA_MODEL) | — |
| S10 SEC-3 | nit | Los logs `idempotency_*` de `POST /users` incluyen el sufijo del scope (huella SHA-256 del body); loguear el scope base o usar HMAC con secreto | **S15** (logs) |
| S16 SEC-1 | minor | Acciones de terceros pinneadas por tag mayor: si se mueve el tag de `astral-sh/setup-uv`, podría alterar el HTML de cobertura que se publica en Pages. Fix: pinnear por SHA con la versión en comentario + Dependabot para `github-actions` | S17 |
| S16 CR-1 | minor | El primer deploy de Pages requiere Pages habilitado con source "GitHub Actions" (el `GITHUB_TOKEN` no puede habilitarlo) | paso único al hacer público el repo |
| S12b CR-2 | nit | Tests de lock/carrera de S12b: si una regresión deja un PATCH colgado, el cleanup puede bloquearse en vez de fallar (agregar `statement_timeout` en el cleanup o cancelar la task) | S17 (S16 ya mergeada) |
