# Requerimientos — Sistema de Alertas Climáticas

Owner: PM · Estado: **Aprobado para desarrollo** (pendiente de sign-off del usuario) · 2026-11-16

## 1. Contexto

Agrobot es un sistema de gestión agrícola con interfaz de WhatsApp (Python, FastAPI, PostgreSQL).
Ya existe un job de ingesta meteorológica que persiste, **para cada día futuro, una entrada por cada
evento climático con su probabilidad**. Este proyecto agrega alertas configurables por el usuario
sobre esos datos.

## 2. Objetivo

Que un productor configure "avisame si hay ≥ X% de probabilidad de `<evento>` en `<campo>`" y reciba
una notificación cuando los pronósticos almacenados superen ese umbral.

## 3. Alcance

**Incluido**
- API REST para usuarios, campos, alertas, consulta de pronósticos y del historial de notificaciones.
- Background job que evalúa periódicamente los pronósticos contra los umbrales y genera notificaciones.
- Envío de notificaciones mediante una interfaz `Notifier` intercambiable, con implementación de log.
- Tabla de datos meteorológicos y **mock** del job de ingesta (proveedor determinístico + upsert).
- Migraciones Alembic, tests, README, CI (GitHub Actions).

**Excluido**
- Integración real con WhatsApp (el enunciado lo excluye explícitamente).
- Autenticación real (se simula con el header `X-User-Id`).
- Deploy a un entorno cloud (se acordó **solo CI**).
- UI.

## 4. Requerimientos funcionales

| ID | Requerimiento | Fuente |
|----|---------------|--------|
| RF-01 | El usuario puede registrar campos (nombre, latitud, longitud). | enunciado ("alertas sobre sus campos") |
| RF-02 | El usuario puede crear una alerta sobre un campo propio indicando tipo de evento y umbral. | enunciado |
| RF-03 | El usuario puede listar, ver, modificar (umbral, activa/inactiva) y eliminar sus alertas. | inferido: gestión mínima |
| RF-04 | El usuario puede consultar los pronósticos almacenados de sus campos. | inferido: contexto para elegir el umbral |
| RF-05 | Un background job evalúa **periódicamente** los pronósticos almacenados contra las alertas activas. | enunciado |
| RF-06 | Una alerta se dispara cuando `probabilidad >= umbral` para el mismo campo y evento, en **cualquier día desde hoy en adelante**. | enunciado ("a partir del cual"); sin horizonte (decisión §6) |
| RF-07 | Al dispararse, se notifica al usuario dueño del campo con un mensaje legible (evento, campo, fecha, probabilidad, umbral). | enunciado |
| RF-08 | **Una notificación por alerta, día pronosticado y episodio** (D1): ni re-ejecuciones del job ni réplicas concurrentes generan duplicados; un pronóstico que se mantiene arriba del umbral no repite el aviso. | decisión §6 |
| RF-09 | Las notificaciones fallidas se reintentan con backoff exponencial hasta un máximo; luego quedan como `failed`. | production-ready |
| RF-10 | El usuario puede consultar su historial de notificaciones (filtrable por estado y alerta). | inferido: trazabilidad |
| RF-11 | Un usuario no puede ver ni operar sobre recursos de otro (404). | production-ready |

## 5. Requerimientos no funcionales

| ID | Requerimiento |
|----|---------------|
| RNF-01 | **Asincronía** end-to-end: FastAPI async, SQLAlchemy 2.0 async + asyncpg, worker asyncio. Nada bloqueante en el event loop. |
| RNF-02 | **Modelo de datos sólido**: integridad en la base (FKs, UNIQUE, CHECK), índices para las queries del job, naming convention. Ver `DATA_MODEL.md`. |
| RNF-03 | **Idempotencia y concurrencia**: el job puede correr N veces o en N réplicas sin duplicar ni enviar dos veces (`ON CONFLICT`, `FOR UPDATE SKIP LOCKED`). |
| RNF-04 | **Resiliencia**: un error transitorio no mata el worker; timeouts en el envío; graceful shutdown con SIGTERM. |
| RNF-05 | **Configuración 12-factor** por variables de entorno. |
| RNF-06 | **Observabilidad**: logs estructurados (key=value) en cada ciclo del job; healthcheck con ping a la DB. |
| RNF-07 | **Calidad**: ruff + mypy strict + pytest contra Postgres real; migraciones testeadas (upgrade/downgrade + `alembic check`). |
| RNF-08 | **Seguridad**: queries parametrizadas, validación estricta de input (`extra="forbid"`), ownership en todas las queries, sin secretos en el repo, contenedor con usuario no-root, teléfonos enmascarados en logs. |
| RNF-09 | Correr localmente con un comando (`docker compose up`) y documentado en el README. |

## 6. Decisiones de producto (acordadas con el usuario)

| # | Tema | Decisión | Alternativa descartada / futura |
|---|------|----------|---------------------------------|
| D1 | Re-notificación | **Por episodio** (S12b): se avisa cada vez que el pronóstico de una alerta y día **cruza el umbral hacia arriba**; mientras siga arriba no se repite. Para volver a avisar, la probabilidad tiene que bajar por debajo de `umbral - REARM_MARGIN` (margen anti-oscilación, default 10 puntos; nivel mínimo 1). Cambiar el umbral o reactivar la alerta la re-evalúa desde cero. | Re-notificar solo si sube ≥ N puntos. |
| D2 | Horizonte de evaluación | **Configurable** `ALERT_HORIZON_DAYS` (default 7; 0 = sin límite) (S12a): los pronósticos lejanos son poco confiables y generan ruido. | Horizonte por alerta. |
| D3 | Granularidad de pronósticos | Por campo (`weather_forecasts.field_id`). | Tabla `locations` compartida entre campos cercanos. |
| D4 | Umbral | Entero 1..100 (%); dispara con `>=`. | — |
| D5 | Alertas por campo | **Varias por (campo, evento) con umbrales distintos** (ej. 50 = atención, 80 = crítico); única por (campo, evento, umbral) entre alertas no borradas (S9). | — |
| D6 | Eventos | `frost`, `rain`, `hail`, `strong_wind`, `heat_wave` (extensibles: VARCHAR + CHECK). | — |
| D7 | Canal | `Notifier` Protocol; implementación `LoggingNotifier`. | Adaptador WhatsApp (fuera de alcance). |
| D8 | Auth | Header `X-User-Id` simulado. | Identidad provista por el gateway de WhatsApp. |
| D9 | "Hoy" | **Fecha local del campo** según su zona IANA (`fields.timezone`, default `America/Argentina/Buenos_Aires`) (S12a). Las zonas `Etc/GMT±N` siguen la convención POSIX de signo invertido (`Etc/GMT+12` = UTC−12). | — |
| D10 | Entrega | At-least-once (puede duplicarse solo si el proceso muere entre el envío y el commit). | Idempotency key en el proveedor. |
| D11 | Borrado de alertas | **Lógico** (`deleted_at`): DELETE idempotente, el historial de notificaciones se conserva y las pendientes se cancelan (S9). | Borrado físico (pierde el historial). |
| D12 | Validación al enviar | El dispatcher re-verifica contra la alerta **actual** antes de enviar (activa, no borrada, probabilidad ≥ umbral vigente); si ya no corresponde, la notificación queda `cancelled` (S9). | — |
| D13 | Reintentos de clientes | Header `Idempotency-Key` **obligatorio en `POST /alerts`**: misma clave + mismo body → misma respuesta; misma clave + otro body → 422; vigencia 24 h (S10, S11b). `users` y `fields` resuelven duplicados por unicidad natural (409). | Clave opcional en todos los POST. |
| D14 | Pronósticos desactualizados | Cada pronóstico guarda `issued_at` del proveedor; un dato más viejo nunca pisa a uno más nuevo; el evaluador ignora pronósticos con más de `MAX_FORECAST_AGE_HOURS` (default 24) y lo reporta (S11). | — |
| D15 | Envío agrupado | Un mensaje por usuario y lote con todas sus notificaciones vencidas (digest; con una sola, el texto de siempre), cada una con su estado individual. **Horario de silencio por usuario** (`PATCH /users/me`, hora local en `users.timezone`): durante el silencio no se envía nada y todo sale junto al terminar, sin excepción para alertas urgentes; si el día pronosticado ya pasó, la notificación se cancela (`forecast_date_passed`) (S13). | Un mensaje por notificación; silencio global por configuración; dejar pasar las urgentes. |
| D16 | Límites y operación | Cuotas configurables: `MAX_FIELDS_PER_USER` (default 20) y `MAX_ALERTS_PER_FIELD` (default 10), excedidas → 409 (S14a). Body de request de hasta 64 KB (413) y 422 con a lo sumo 20 errores. Timeouts de statement y de lock en Postgres; un timeout en la API → 503 reintentable (S14a). Una notificación `failed` es definitiva: no se reintenta (ni a mano ni automáticamente). | Reintento de `failed` (manual con un endpoint o automático); job de retención de datos viejos. |

## 7. Supuestos

- El job de ingesta existente escribe en `weather_forecasts` con upsert por (campo, día, evento).
- Las probabilidades vienen como porcentaje 0..100.
- La frecuencia del job es configurable (`WORKER_INTERVAL_SECONDS`, default 60s).

## 8. Criterios de aceptación globales

- `docker compose up --build` levanta db + migraciones + api + worker sin pasos manuales.
- `make seed && make jobs-once` genera notificaciones `sent` visibles en `GET /api/v1/notifications`.
- `make check` (lint + typecheck + tests) en verde, y CI en verde en cada PR.
- README explica cómo correrlo, las decisiones y lo que quedó afuera.

## 9. Trazabilidad

Cada historia de `BACKLOG.md` referencia los RF/RNF que cubre; cada PR referencia su historia.
