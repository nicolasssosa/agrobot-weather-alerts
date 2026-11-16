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
| RF-08 | **Una sola notificación por alerta y día pronosticado**: ni re-ejecuciones del job ni cambios posteriores del pronóstico generan duplicados. | decisión §6 |
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
| D1 | Re-notificación | Una vez por (alerta, día). | Re-notificar si la probabilidad sube ≥ N puntos o si cambia el umbral. |
| D2 | Horizonte de evaluación | **Sin horizonte**: todos los días ≥ hoy (el enunciado no lo define). | Horizonte global o por alerta (`lookahead_days`). |
| D3 | Granularidad de pronósticos | Por campo (`weather_forecasts.field_id`). | Tabla `locations` compartida entre campos cercanos. |
| D4 | Umbral | Entero 1..100 (%); dispara con `>=`. | — |
| D5 | Alertas por campo | Una por (campo, evento); el umbral se cambia con PATCH. | Varias alertas por evento con distintos umbrales. |
| D6 | Eventos | `frost`, `rain`, `hail`, `strong_wind`, `heat_wave` (extensibles: VARCHAR + CHECK). | — |
| D7 | Canal | `Notifier` Protocol; implementación `LoggingNotifier`. | Adaptador WhatsApp (fuera de alcance). |
| D8 | Auth | Header `X-User-Id` simulado. | Identidad provista por el gateway de WhatsApp. |
| D9 | "Hoy" | Fecha UTC del worker. | Zona horaria por usuario. |
| D10 | Entrega | At-least-once (puede duplicarse solo si el proceso muere entre el envío y el commit). | Idempotency key en el proveedor. |

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
