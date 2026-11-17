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

## S12a — Zona horaria por campo y horizonte configurable · D2, D9
- [ ] `fields.timezone` (IANA, validada; default `America/Argentina/Buenos_Aires`), en `FieldCreate` (opcional) y `FieldRead`; migración 0007.
- [ ] El evaluador calcula "hoy" **por campo** en su zona horaria y evalúa `[hoy_local, hoy_local + ALERT_HORIZON_DAYS)` (0 = sin límite).
- [ ] Follow-ups de S11: los scripts `ingest`/`seed` usan `FORECAST_FUTURE_TOLERANCE_MINUTES` del setting (SEC-3); warning `future_forecasts count=<n> fields=<m>` cuando el evaluador excluye pronósticos con `issued_at` adelantado (SEC-4); la validación al enviar compara contra la probabilidad del **pronóstico vigente** (SEC-5).

## S12b — Re-aviso por episodio · D1
- [ ] Estado por (alerta, día): re-aviso en cada cruce hacia arriba, margen anti-oscilación, cambio de umbral re-evalúa.
- [ ] **Reactivar** una alerta (`is_active` false → true) también re-evalúa (QA-1 de S9).
- [ ] Sigue siendo set-based en SQL; la regla de transición se documenta y se testea también como función pura de referencia (tests de equivalencia).

## S13 — Envío agrupado por usuario + horario de silencio · D15
- [ ] Un mensaje por usuario y ciclo con todas sus notificaciones; cada notificación conserva su estado individual. Horario de silencio configurable: se difiere el envío hasta que termine.

## S14 — Operación · D16
- [ ] Advisory lock del evaluador (una réplica evalúa por vez); `statement_timeout`/`lock_timeout`; cuotas por usuario; notificaciones `failed` reintentables; job de retención (pronósticos pasados, claves de idempotencia vencidas); límite de tamaño de request y de cantidad de errores en el 422 (follow-ups S4).

## S15 — Observabilidad
- [ ] Logs JSON estructurados con `request_id`; métricas Prometheus (`/metrics`); `GET /health/live` y `/health/ready` (DB + heartbeat del worker).

## S16 — CI (GitHub Actions) + pre-commit + cobertura · RNF-07
Archivos: `.github/workflows/ci.yml`.
- [ ] Trigger en `pull_request` y `push` a `main`.
- [ ] Job con service `postgres:16-alpine` (crea `agrobot_test`), `astral-sh/setup-uv`, `uv sync --frozen`, `ruff check`, `ruff format --check`, `mypy app`, `pytest`.
- [ ] Job `docker build` (sin push) para validar el Dockerfile.
- [ ] Permisos mínimos (`permissions: contents: read`), acciones pinneadas por versión mayor, cache de uv.
- [ ] Job que verifica el contrato OpenAPI (corre el test de contrato de S8).
- [ ] Follow-up S7 SEC-3: `worker --once` le pasa `stop_event` a `run_cycle` (+ test).
- [ ] Follow-up S7 SEC-5: declarar `ENVIRONMENT` explícito en el `app-env` del compose y que el seed exija `ENVIRONMENT` definida (o flag `--yes-demo`).

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
