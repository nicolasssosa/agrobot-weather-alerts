<img src="assets/icon.svg" width="64" height="64" alt="">

# Agrobot — Sistema de Alertas Climáticas

[![CI](https://github.com/nicolasssosa/agrobot-weather-alerts/actions/workflows/ci.yml/badge.svg)](https://github.com/nicolasssosa/agrobot-weather-alerts/actions/workflows/ci.yml)
· [API (Swagger UI)](https://nicolasssosa.github.io/agrobot-weather-alerts/)
· [Cobertura](https://nicolasssosa.github.io/agrobot-weather-alerts/coverage/)

Los productores configuran alertas sobre sus campos (*"avisame si hay ≥ 70% de probabilidad de
helada en Lote Norte"*). Un worker compara periódicamente los pronósticos guardados con los
umbrales y le manda al usuario **un solo mensaje** con todo lo que superó su umbral:

```text
⚠️ Tenés 2 alertas climáticas:
• Helada en "Lote Norte": 85% de probabilidad para el 18/11/2026 (tu umbral: 70%).
• Granizo en "Lote Sur": 60% de probabilidad para el 19/11/2026 (tu umbral: 50%).
```

**Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.0 async (asyncpg) · PostgreSQL 16 · Alembic ·
pytest · uv · Docker · GitHub Actions.

**En números:** 14 endpoints · 9 migraciones · ~1.180 tests contra Postgres real · 95% de cobertura
(mínimo exigido en CI: 90%) · ruff + mypy strict.

---

## Índice

- [Inicio rápido (un comando)](#inicio-rápido-un-comando)
- [Cómo levantar el proyecto](#cómo-levantar-el-proyecto)
  - [Requisitos](#requisitos)
  - [Opción A: todo con Docker](#opción-a-todo-con-docker)
  - [Opción B: desarrollo local](#opción-b-desarrollo-local)
  - [Probar la API paso a paso](#probar-la-api-paso-a-paso)
  - [Tests y calidad](#tests-y-calidad)
  - [Comandos `make`](#comandos-make)
  - [Problemas comunes](#problemas-comunes)
- [Cómo funciona](#cómo-funciona)
  - [Arquitectura](#arquitectura)
  - [Flujo de punta a punta](#flujo-de-punta-a-punta)
  - [Episodios: cuándo se vuelve a avisar](#episodios-cuándo-se-vuelve-a-avisar)
  - [Ciclo de vida de una notificación](#ciclo-de-vida-de-una-notificación)
  - [Modelo de datos](#modelo-de-datos)
  - [Garantías de idempotencia y concurrencia](#garantías-de-idempotencia-y-concurrencia)
- [Configuración](#configuración)
- [Estructura del repo](#estructura-del-repo)
- [Decisiones principales](#decisiones-principales)
- [Trade-offs](#trade-offs)
- [Limitaciones conocidas](#limitaciones-conocidas)
- [Próximos pasos](#próximos-pasos)
- [CI, GitHub Pages y proceso](#ci-github-pages-y-proceso)
- [Documentación](#documentación)

---

## Inicio rápido (un comando)

Con Docker instalado:

```bash
make demo
```

Levanta Postgres, corre las migraciones, arranca la API y el worker, carga datos de demo (2
usuarios, 3 campos, 4 alertas y 14 días de pronósticos mock), corre un ciclo del job y muestra las
notificaciones enviadas. Es idempotente: se puede correr varias veces.

Después:

- Swagger UI: <http://localhost:8000/docs> → botón **Authorize** → `X-User-Id: 1` (Ana) o `2` (Bruno).
- Los mensajes "enviados" se ven en los logs del worker: `docker compose logs -f worker`.
- Para apagar todo y borrar la base: `make down`.

---

## Cómo levantar el proyecto

### Requisitos

| Herramienta | Versión | Para qué |
|-------------|---------|----------|
| Docker + Docker Compose | Compose v2 | Postgres y el stack completo |
| `make`, `curl` | cualquiera | atajos y ejemplos |
| [uv](https://docs.astral.sh/uv/) | ≥ 0.5 | solo para desarrollo local (instala Python 3.12 solo si falta) |

Puertos que usa: **5432** (Postgres) y **8000** (API).

### Opción A: todo con Docker

```bash
docker compose up --build -d --wait api worker   # o `make up` en primer plano
```

Qué levanta (`docker-compose.yml`):

| Servicio | Qué hace | Cuándo arranca |
|----------|----------|----------------|
| `db` | Postgres 16. En el primer arranque crea las bases `agrobot` y `agrobot_test` | primero; tiene healthcheck |
| `migrate` | `alembic upgrade head` y termina | cuando `db` está sano |
| `api` | `uvicorn app.main:app` en `:8000` (usuario no-root) | cuando `migrate` terminó bien |
| `worker` | `python -m app.jobs.worker`: evalúa y despacha cada 30 s | cuando `migrate` terminó bien |

Verificar y cargar datos:

```bash
curl -s localhost:8000/health                                  # {"status":"ok"}
docker compose exec api python -m app.scripts.seed             # usuarios, campos, alertas y pronósticos demo
docker compose exec api python -m app.scripts.ingest --days 14 # otra pasada de ingesta mock (todos los campos)
docker compose exec worker python -m app.jobs.worker --once    # forzar un ciclo ya, sin esperar los 30 s
docker compose logs -f worker                                  # ver los mensajes "enviados"
```

Apagar: `docker compose down` (conserva los datos) o `make down` (borra el volumen).

### Opción B: desarrollo local

La API y el worker corren en tu máquina con autoreload; Postgres sigue en Docker.

```bash
uv sync                  # dependencias (incluye las de desarrollo)
cp .env.example .env     # configuración local; ENVIRONMENT=local habilita el seed
make db-up               # solo Postgres en :5432 (crea agrobot y agrobot_test)
make migrate             # alembic upgrade head sobre la base de desarrollo
make seed                # datos demo + 14 días de pronósticos mock
make api                 # http://localhost:8000/docs (con --reload)
make worker              # en otra terminal: el job en loop
# o bien:
make jobs-once           # un solo ciclo de evaluación + despacho y sale
```

`.env.example` documenta todas las variables con su default; los defaults ya apuntan al Postgres
del compose, así que el `.env` es opcional salvo para el seed (ver [Problemas comunes](#problemas-comunes)).

### Probar la API paso a paso

Todo lo que no sea `POST /users` o `/health` requiere el header `X-User-Id` (autenticación
simulada; en producción la identidad la da el gateway de WhatsApp).

```bash
API=http://localhost:8000/api/v1

# 1. Crear un usuario (público)
curl -s -X POST $API/users -H 'content-type: application/json' \
  -d '{"phone_number":"+5491155550000","name":"Carla"}'
# → {"id": 3, ...}

# 2. Registrar un campo
curl -s -X POST $API/fields -H 'X-User-Id: 3' -H 'content-type: application/json' \
  -d '{"name":"Lote Este","latitude":-33.0,"longitude":-60.0}'
# → {"id": 4, "timezone": "America/Argentina/Buenos_Aires", ...}

# 3. Crear una alerta (Idempotency-Key obligatoria: un reintento con la misma clave no duplica)
curl -s -X POST $API/alerts -H 'X-User-Id: 3' -H "Idempotency-Key: $(uuidgen)" \
  -H 'content-type: application/json' -d '{"field_id":4,"event_type":"rain","threshold":40}'

# 4. Traer pronósticos para el campo nuevo y correr un ciclo del job
make ingest && make jobs-once          # en Docker: los comandos `docker compose exec` de la opción A

# 5. Ver los pronósticos y el historial de notificaciones
curl -s "$API/fields/4/forecasts?event_type=rain" -H 'X-User-Id: 3'
curl -s "$API/notifications?status=sent" -H 'X-User-Id: 3'

# 6. Opcional: horario de silencio (no se envía nada entre las 22:00 y las 07:00 locales)
curl -s -X PATCH $API/users/me -H 'X-User-Id: 3' -H 'content-type: application/json' \
  -d '{"quiet_hours":{"start":"22:00","end":"07:00"}}'
```

| Recurso | Endpoints |
|---------|-----------|
| health | `GET /health` |
| users | `POST /users` · `GET /users/me` · `PATCH /users/me` |
| fields | `POST /fields` · `GET /fields` · `GET /fields/{id}` · `GET /fields/{id}/forecasts` |
| alerts | `POST /alerts` · `GET /alerts` · `GET /alerts/{id}` · `PATCH /alerts/{id}` · `DELETE /alerts/{id}` |
| notifications | `GET /notifications` |

Contrato completo en [`docs/API.md`](docs/API.md) y en [`docs/openapi.json`](docs/openapi.json)
(versionado; un test falla si difiere de lo que expone la app).
**Postman / Insomnia / Bruno:** importar `docs/openapi.json` genera la colección completa con
ejemplos válidos en cada request.

### Tests y calidad

```bash
make db-up        # los tests usan la base agrobot_test (Postgres real, sin mocks de la DB)
make check        # ruff + ruff format --check + mypy strict + pytest
make cov          # tests con cobertura: resumen en terminal + HTML en htmlcov/
```

- Las migraciones se testean en cada corrida: `downgrade base` + `upgrade head` y `alembic check`.
- Fechas y horas fijas en los tests de jobs (nunca el reloj real).
- Tests de concurrencia con conexiones separadas (dos evaluadores, dos dispatchers, PATCH vs evaluador).
- Hooks opcionales de pre-commit (ruff): `uv tool install pre-commit && pre-commit install`.

### Comandos `make`

| Comando | Qué hace |
|---------|----------|
| `make demo` | Stack en Docker + seed + un ciclo del job + notificaciones enviadas |
| `make up` / `make down` | Stack completo en primer plano / bajarlo **y borrar el volumen** |
| `make db-up` / `make db-down` | Solo Postgres / bajar los contenedores |
| `make migrate` | `alembic upgrade head` sobre `DATABASE_URL` |
| `make migration m="..."` | Nueva migración vacía |
| `make seed` | Datos demo (requiere `ENVIRONMENT` explícita) |
| `make ingest` | Una pasada de la ingesta mock (14 días, todos los campos) |
| `make api` / `make worker` / `make jobs-once` | API con reload / worker en loop / un ciclo |
| `make check` / `make cov` | Definition of done local / cobertura |
| `make lint` / `make fmt` / `make typecheck` | ruff / ruff con fix + format / mypy |
| `make openapi` | Regenera `docs/openapi.json` |

### Problemas comunes

| Síntoma | Causa y solución |
|---------|------------------|
| `port is already allocated` en 5432 u 8000 | Otro Postgres/servicio local. Pararlo, o cambiar el puerto publicado en `docker-compose.yml` y `DATABASE_URL`. |
| Tests: `database "agrobot_test" does not exist` | El volumen se creó antes del script de init. `docker compose exec db psql -U postgres -c "CREATE DATABASE agrobot_test"` o `make down && make db-up`. |
| `seed abortado: ENVIRONMENT no está definida explícitamente` | Guarda de seguridad del seed. `cp .env.example .env` (trae `ENVIRONMENT=local`) o `make seed` con `ENVIRONMENT=local`. En `staging`/`production` el seed se niega siempre. |
| `make jobs-once` no genera nada | No hay pronósticos frescos (se ignoran los de más de 24 h) o ninguno supera los umbrales: `make ingest` y repetir. |
| `POST /alerts` → 422 `Idempotency-Key header is required` | Es obligatoria en ese endpoint: agregar `-H "Idempotency-Key: $(uuidgen)"`. |
| 409 `Field limit reached for this user` / `Alert limit reached for this field` | Cuotas `MAX_FIELDS_PER_USER` (20) y `MAX_ALERTS_PER_FIELD` (10). |
| 503 `Database busy, try again later` | Un lock o una consulta superó `DB_LOCK_TIMEOUT_MS` / `DB_STATEMENT_TIMEOUT_MS`. Es reintentable (header `Retry-After`). |

---

## Cómo funciona

### Arquitectura

```mermaid
flowchart LR
    P([Productor<br/>vía WhatsApp])
    subgraph API["API · FastAPI (N réplicas)"]
        R[routers<br/>HTTP + schemas] --> S[services<br/>reglas + queries]
    end
    subgraph W["Worker · asyncio (N réplicas)"]
        EV[Evaluador<br/>set-based SQL]
        DSP[Dispatcher<br/>patrón lease]
    end
    ING[[Ingesta de pronósticos<br/>job existente · mock]]
    DB[(PostgreSQL 16)]
    N[[Notifier<br/>LoggingNotifier · WhatsApp a futuro]]

    P -- "REST + X-User-Id" --> R
    S <--> DB
    ING -- "UPSERT weather_forecasts" --> DB
    EV -- "alert_day_states<br/>+ notifications pending" --> DB
    DSP -- "SKIP LOCKED + lease" --> DB
    DSP -- "un mensaje por usuario" --> N
    N -. mensaje .-> P
```

- **API y worker son procesos separados** que solo comparten la base: escalan por separado y un
  deploy o crash de uno no corta al otro.
- **Postgres es la cola y el árbitro de concurrencia**: `ON CONFLICT` para idempotencia,
  `FOR UPDATE SKIP LOCKED` para repartir trabajo entre réplicas y un advisory lock para el
  evaluador. Sin Redis, Celery ni brokers.
- `notifications` funciona como **outbox**: el evaluador solo escribe filas `pending`; el envío es
  otro paso, con reintentos.
- El canal es un `Protocol` (`app/notifiers/base.py`): cambiar el log por WhatsApp es agregar una
  implementación, sin tocar el dispatcher.

### Flujo de punta a punta

```mermaid
sequenceDiagram
    autonumber
    actor P as Productor
    participant API as API (FastAPI)
    participant DB as PostgreSQL
    participant ING as Ingesta (mock)
    participant EV as Worker · Evaluador
    participant DSP as Worker · Dispatcher
    participant N as Notifier (log / WhatsApp)

    rect rgba(59, 130, 246, 0.08)
    Note over P,DB: 1 · Configuración (API)
    P->>API: POST /users (público)
    API->>DB: INSERT users (teléfono E.164 UNIQUE)
    API-->>P: 201
    P->>API: POST /fields (X-User-Id)
    API->>DB: lock del usuario + cuota MAX_FIELDS_PER_USER + INSERT fields
    API-->>P: 201 (o 409 cuota / nombre repetido)
    P->>API: POST /alerts (X-User-Id, Idempotency-Key)
    API->>DB: advisory lock de (usuario, clave) + busca en idempotency_keys
    alt clave ya usada con el mismo body
        API-->>P: 201 replay byte a byte (Idempotent-Replayed: true)
    else misma clave con otro body
        API-->>P: 422
    else clave nueva
        API->>DB: INSERT alerts + guarda la respuesta (misma transacción)
        API-->>P: 201 Created (o 409 duplicada / tope MAX_ALERTS_PER_FIELD)
    end
    end

    rect rgba(16, 185, 129, 0.08)
    Note over ING,DB: 2 · Ingesta de pronósticos (job existente, mockeado)
    ING->>DB: UPSERT weather_forecasts por (campo, día, evento)<br/>solo si EXCLUDED.issued_at es más nuevo
    end

    rect rgba(245, 158, 11, 0.08)
    Note over EV,DB: 3 · Evaluación (cada WORKER_INTERVAL_SECONDS, una transacción)
    EV->>DB: pg_advisory_xact_lock en cola (espera máx. EVALUATOR_LOCK_WAIT_MS)
    alt otra réplica sigue evaluando
        EV-->>EV: evaluation_skipped (warning) y sigue al despacho
    else lock adquirido
        Note right of EV: candidatos = alertas activas × pronóstico vigente<br/>en [hoy local del campo, + ALERT_HORIZON_DAYS)<br/>con issued_at fresco (MAX_FORECAST_AGE_HOURS)
        EV->>DB: UPDATE alert_day_states SET armed = true<br/>donde bajó de umbral − REARM_MARGIN (re-armado)
        EV->>DB: UPSERT alert_day_states (episodio + 1, armed = false)<br/>solo donde probability ≥ threshold y estaba armada
        EV->>DB: INSERT notifications pending<br/>ON CONFLICT (alert_id, forecast_date, episode) DO NOTHING
        EV->>DB: COMMIT (libera el lock)
    end
    end

    rect rgba(168, 85, 247, 0.08)
    Note over DSP,N: 4 · Despacho (mismo ciclo, en lotes hasta vaciar la cola)
    DSP->>DB: TX 1: usuarios con pendientes vencidas<br/>FOR NO KEY UPDATE SKIP LOCKED (≤ DISPATCH_BATCH_SIZE)
    DSP->>DB: sus notificaciones (≤ DIGEST_MAX_ITEMS c/u)<br/>FOR UPDATE SKIP LOCKED
    alt ya no corresponde (alerta borrada / pausada, umbral subió,<br/>día pasado, pronóstico viejo)
        DSP->>DB: cancelled + motivo
    else usuario en horario de silencio
        DSP->>DB: sigue pending, next_attempt_at = fin del silencio
    else enviar
        DSP->>DB: lease: next_attempt_at = now + DISPATCH_LEASE_SECONDS
    end
    DSP->>DB: COMMIT (sin locks durante el envío)
    DSP->>N: send() · un mensaje por usuario (digest si son varias)<br/>fuera de transacción, timeout + concurrencia acotada
    DSP->>DB: TX 2: resultado por notificación, solo si el lease sigue siendo suyo
    alt envío OK
        DSP->>DB: sent (sent_at, attempts + 1)
    else error y quedan intentos
        DSP->>DB: pending, next_attempt_at = now + 30 s × 2^attempts
    else error y attempts ≥ DISPATCH_MAX_ATTEMPTS
        DSP->>DB: failed (last_error con el teléfono enmascarado)
    end
    end

    rect rgba(100, 116, 139, 0.08)
    Note over P,DB: 5 · Cambios y consulta
    P->>API: PATCH /alerts/{id} (cambia umbral o reactiva)
    API->>DB: lock compartido del evaluador + reset de alert_day_states<br/>+ cancela pendientes no arrendadas (alert_reset)
    API-->>P: 200 (el próximo ciclo re-evalúa desde cero)
    P->>API: GET /notifications?status=sent
    API-->>P: 200 (episode, probability, threshold, status, sent_at)
    end
```

Puntos clave del flujo:

- **El evaluador no trae filas a Python**: re-armado y disparo son dos sentencias SQL sobre todo el
  conjunto, así el costo no crece con loops en la aplicación.
- **"Hoy" es por campo**: se calcula en la zona IANA de cada campo, así a las 22:00 de Buenos Aires
  (01:00 UTC del día siguiente) el día local sigue siendo el de hoy.
- **Nunca se espera a la red con una transacción abierta**: el dispatcher reclama y arrienda en
  milisegundos, envía sin locks y registra el resultado en otra transacción corta. Si el proceso
  muere a mitad del envío, el lease vence y otra réplica reintenta (at-least-once).
- **La notificación se re-valida al enviarla** contra la alerta y el pronóstico actuales: nunca se
  manda un aviso que ya no corresponde.

### Episodios: cuándo se vuelve a avisar

Estado por (alerta, día pronosticado) en `alert_day_states`. Con umbral 70 y `REARM_MARGIN` 10:
avisa al llegar a 70, y para volver a avisar el pronóstico tiene que bajar de 60 y volver a subir.
La banda 60–69 evita avisos repetidos por oscilaciones chicas.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Armada: sin fila (episodio 0)
    Armada --> Disparada: probability ≥ threshold<br/>notifica episodio e + 1
    Disparada --> Disparada: sigue ≥ umbral − margen<br/>(no repite)
    Disparada --> Armada: probability < umbral − margen<br/>(re-armado, no notifica)
    Disparada --> Armada: PATCH cambia el umbral<br/>o reactiva la alerta
```

La misma regla está implementada como función pura (`app/jobs/episodes.py`) y los tests comparan
secuencias aleatorias con semilla fija contra el evaluador SQL: mismas notificaciones y mismo
estado final.

### Ciclo de vida de una notificación

```mermaid
stateDiagram-v2
    [*] --> pending: el evaluador dispara un episodio
    pending --> pending: arrendada (lease) mientras se envía<br/>diferida por horario de silencio<br/>reintento con backoff exponencial
    pending --> sent: envío OK
    pending --> failed: error con attempts ≥ DISPATCH_MAX_ATTEMPTS
    pending --> cancelled: alert_deleted · alert_inactive · alert_reset<br/>forecast_date_passed · below_current_threshold · stale_forecast
    sent --> [*]
    failed --> [*]
    cancelled --> [*]
```

`failed` es definitivo: no se reintenta. `last_error` y `next_attempt_at` son internos y no se
exponen en la API.

### Modelo de datos

```mermaid
erDiagram
    users ||--o{ fields : "tiene"
    users ||--o{ idempotency_keys : "reintentos de POST /alerts"
    fields ||--o{ weather_forecasts : "pronóstico por día y evento"
    fields ||--o{ alerts : "configura"
    alerts ||--o{ alert_day_states : "estado de episodio por día"
    alerts ||--o{ notifications : "outbox"

    users {
        bigint id PK
        varchar phone_number UK "E.164 (CHECK)"
        varchar timezone "IANA"
        time quiet_hours_start "nullable"
        time quiet_hours_end "nullable"
    }
    fields {
        bigint id PK
        bigint user_id FK
        varchar name "UNIQUE por usuario"
        numeric latitude
        numeric longitude
        varchar timezone "define el hoy local"
    }
    weather_forecasts {
        bigint id PK
        bigint field_id FK
        date forecast_date
        varchar event_type "UNIQUE (field, date, event)"
        smallint probability "0..100"
        timestamptz issued_at "frescura"
    }
    alerts {
        bigint id PK
        bigint field_id FK
        varchar event_type
        smallint threshold "1..100"
        boolean is_active
        timestamptz deleted_at "borrado lógico"
    }
    alert_day_states {
        bigint alert_id FK
        date forecast_date "UNIQUE (alert, date)"
        int episode
        boolean armed
    }
    notifications {
        bigint id PK
        bigint alert_id FK
        date forecast_date
        int episode "UNIQUE (alert, date, episode)"
        smallint probability "snapshot"
        smallint threshold "snapshot"
        varchar status "pending sent failed cancelled"
        smallint attempts
        timestamptz next_attempt_at "backoff y lease"
    }
    idempotency_keys {
        bigint id PK
        bigint user_id FK
        varchar key "UNIQUE (user, scope, key)"
        char request_hash
        timestamptz expires_at
    }
```

Integridad en la base, no solo en la app: FKs con `ON DELETE CASCADE`, CHECKs de rangos, E.164 y
enums, UNIQUEs que son la deduplicación real, e índices parciales para los caminos calientes
(`alerts` activas para el evaluador, `notifications` pendientes para el dispatcher). Detalle en
[`docs/DATA_MODEL.md`](docs/DATA_MODEL.md).

### Garantías de idempotencia y concurrencia

| Riesgo | Mecanismo |
|--------|-----------|
| El job corre dos veces o en dos réplicas y duplica avisos | Advisory lock del evaluador + `UPSERT ... WHERE armed` + `UNIQUE (alert_id, forecast_date, episode)` |
| Dos dispatchers mandan lo mismo | `FOR UPDATE SKIP LOCKED` + lease en `next_attempt_at` como token de propiedad |
| Un usuario recibe dos mensajes en el mismo ciclo | Lock de la fila del usuario (`FOR NO KEY UPDATE SKIP LOCKED`) al armar el digest |
| Proveedor colgado deja locks abiertos | Envío fuera de transacción, timeout por envío y concurrencia acotada |
| El cliente reintenta `POST /alerts` por un corte de red | `Idempotency-Key` obligatoria: misma clave + mismo body → misma respuesta |
| Un pronóstico viejo pisa uno nuevo | Upsert condicional por `issued_at` |
| Se avisa algo que ya no aplica | Re-validación al enviar (alerta, umbral vigente, día, frescura) → `cancelled` |
| PATCH y evaluación simultáneos se pisan | Lock compartido (PATCH) vs exclusivo (evaluador) sobre la misma clave |
| SIGTERM corta un lote a la mitad | Graceful shutdown: termina el lote en curso y no arranca otro |

---

## Configuración

12-factor: todo por variables de entorno (o `.env`). Lista completa, con rangos, en
[`.env.example`](.env.example). Las más relevantes:

| Variable | Default | Qué controla |
|----------|---------|--------------|
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/agrobot` | Base de la app |
| `TEST_DATABASE_URL` | `.../agrobot_test` | Base de los tests |
| `ENVIRONMENT` | `local` | `staging`/`production` bloquean el seed |
| `WORKER_INTERVAL_SECONDS` | `60` (30 en el compose) | Frecuencia del ciclo evaluar → despachar |
| `ALERT_HORIZON_DAYS` | `7` | Días hacia adelante que se evalúan (0 = sin límite) |
| `MAX_FORECAST_AGE_HOURS` | `24` | Un pronóstico más viejo no dispara ni se envía |
| `REARM_MARGIN` | `10` | Puntos que tiene que bajar el pronóstico para volver a avisar |
| `DISPATCH_BATCH_SIZE` | `100` | Usuarios (= mensajes) por lote |
| `DIGEST_MAX_ITEMS` | `10` | Notificaciones por mensaje; el resto sale en el ciclo siguiente |
| `DISPATCH_MAX_ATTEMPTS` | `5` | Intentos antes de `failed` |
| `DISPATCH_BACKOFF_BASE_SECONDS` | `30` | Backoff: 30 s, 60 s, 120 s… |
| `NOTIFIER_TIMEOUT_SECONDS` / `DISPATCH_CONCURRENCY` / `DISPATCH_LEASE_SECONDS` | `10` / `10` / `120` | Envío; el lease se valida contra el peor caso del lote |
| `MAX_FIELDS_PER_USER` / `MAX_ALERTS_PER_FIELD` | `20` / `10` | Cuotas (409 al excederlas) |
| `MAX_REQUEST_BODY_BYTES` | `65536` | 413 por encima |
| `DB_STATEMENT_TIMEOUT_MS` / `DB_LOCK_TIMEOUT_MS` | `30000` / `5000` | Timeouts de Postgres (503 reintentable en la API) |
| `IDEMPOTENCY_TTL_HOURS` | `24` | Vigencia de una `Idempotency-Key` |

---

## Estructura del repo

```text
app/
├── api/            routers HTTP, dependencias (auth X-User-Id) y middleware de tamaño de body
├── services/       reglas de negocio y queries (ownership, cuotas, idempotencia); lanzan errores de dominio
├── schemas/        Pydantic v2 (extra="forbid", ejemplos para OpenAPI)
├── models/         SQLAlchemy 2.0 tipado
├── jobs/           worker, evaluator, dispatcher, episodes (función pura), quiet_hours (función pura)
├── notifiers/      Notifier Protocol + LoggingNotifier
├── weather/        proveedor mock determinístico + ingesta con upsert condicional
├── scripts/        seed, ingest, export_openapi
└── core/           config, db (engine + timeouts), errores, logging, enmascarado de PII
migrations/         Alembic (9 migraciones, todas con downgrade)
tests/              api/, jobs/, weather/, migraciones, esquema, contrato OpenAPI, config de CI
docs/               requerimientos, backlog, contratos (API, DATA_MODEL, JOBS, TESTING), proceso
site/               Swagger UI estático que publica GitHub Pages
.agents/            definición de cada rol del equipo de agentes
```

Capas: **router** (HTTP) → **service** (reglas, queries, `commit`) → **modelos**. Los services
lanzan `NotFoundError` / `ConflictError`, nunca `HTTPException`; la traducción a HTTP es global.

---

## Decisiones principales

El detalle y las alternativas descartadas están en
[`REQUIREMENTS.md` §6](docs/REQUIREMENTS.md#6-decisiones-de-producto-acordadas-con-el-usuario).

| # | Tema | Decisión |
|---|------|----------|
| D1 | Re-notificación | Por **episodio**: se avisa cada vez que el pronóstico cruza el umbral hacia arriba, con histéresis (`REARM_MARGIN`) |
| D2 | Horizonte | Configurable, 7 días por default: los pronósticos lejanos son poco confiables |
| D3 | Pronósticos | Por campo (la ingesta resuelve lat/long) |
| D4–D6 | Umbral y eventos | Entero 1..100 con `>=`; varias alertas por evento con umbrales distintos; 5 eventos extensibles |
| D7 | Canal | `Notifier` Protocol con `LoggingNotifier`; WhatsApp fuera de alcance |
| D8 | Auth | Header `X-User-Id` simulado |
| D9 | "Hoy" | Fecha local del campo según su zona IANA |
| D10 | Entrega | At-least-once (el `notification_id` viaja en el mensaje para deduplicar en el proveedor) |
| D11–D12 | Borrado y validación | Borrado lógico de alertas (se conserva el historial); re-validación al enviar |
| D13 | Reintentos de clientes | `Idempotency-Key` obligatoria en `POST /alerts` |
| D14 | Frescura | `issued_at` por pronóstico; los viejos no disparan ni pisan datos nuevos |
| D15 | Envío | Un mensaje por usuario (digest) y horario de silencio por usuario |
| D16 | Límites | Cuotas, tamaño de body, timeouts de DB con 503 reintentable; `failed` es definitivo |

**¿Por qué un worker aparte y no `BackgroundTasks` de FastAPI?** Escala distinto que la API (N
réplicas de API no deben ser N evaluadores), un deploy de la API no corta envíos, y Postgres ya da
cola (`SKIP LOCKED`), exclusión (advisory locks) e idempotencia (`ON CONFLICT`) sin sumar Redis ni
Celery.

---

## Trade-offs

| Elegimos | En lugar de | Ganamos | Pagamos |
|----------|-------------|---------|---------|
| Postgres como cola | Redis / RabbitMQ / Celery | Una sola pieza de infra; encolar y cambiar estado en la misma transacción | Throughput acotado por la base; polling cada N segundos en vez de push |
| Evaluación set-based en SQL | Loop en Python por alerta | Escala con el tamaño de la base, no con round-trips | SQL más complejo; por eso existe la función pura de referencia y los tests de equivalencia |
| Un evaluador a la vez (advisory lock) | Evaluación particionada entre réplicas | Sin episodios duplicados ni carreras con los PATCH | La evaluación no escala horizontalmente (el despacho sí) |
| At-least-once | Exactly-once | Simple y sin estado en el proveedor | Un duplicado posible si el proceso muere entre enviar y registrar |
| Digest por usuario | Un mensaje por notificación | Menos ruido en WhatsApp, menos costo por mensaje | Un fallo de envío afecta a todo el grupo (cada notificación conserva su propio contador) |
| Sin bypass del silencio para alertas urgentes | Dejar pasar las críticas | Regla simple y predecible para el usuario | Una helada inminente puede llegar al terminar el silencio |
| Pronóstico por campo | Grilla de ubicaciones compartida | Modelo simple, sin geoespacial | Pronósticos duplicados para campos vecinos |
| Auth simulada por header | Auth real | Foco en el dominio | No apto para exponer públicamente tal cual |

---

## Limitaciones conocidas

Hallazgos `minor`/`nit` de las reviews que se aceptaron sin corregir (todos con impacto acotado):

- **Entrega at-least-once:** si el worker muere después de enviar y antes de registrar el
  resultado, la notificación se reenvía al vencer el lease.
- **Pausa durante una evaluación en curso:** una corrida que ya tomó su snapshot puede generar el
  aviso de una alerta recién pausada; la re-validación al enviar lo cancela si el dispatcher lo ve
  pausado.
- **DELETE concurrente con el evaluador:** puede abortar ese ciclo de evaluación por FK; el
  siguiente ciclo lo corrige solo.
- **Dos PATCH simultáneos sobre la misma alerta** pueden perder un re-armado (el re-aviso llega
  recién cuando el pronóstico baja del margen) o hacer uno de más.
- **Si el rollback tras `evaluation_failed` también falla** (conexión caída), ese ciclo no despacha;
  el siguiente sí.
- **Seed:** reutiliza un usuario existente con el teléfono demo y no es seguro con dos corridas
  concurrentes (script de demo one-shot).
- **Sin retención:** `weather_forecasts`, `alert_day_states`, `notifications` e `idempotency_keys`
  crecen sin purga (los índices para purgar ya existen).

---

## Próximos pasos

| Prioridad | Qué | Por qué |
|-----------|-----|---------|
| Alta | Adaptador WhatsApp (Cloud API / BSP) implementando `Notifier`, con `notification_id` como clave de idempotencia | Es el canal real; con la clave, la entrega pasa a ser efectivamente exactly-once |
| Alta | Auth real (identidad del gateway de WhatsApp o JWT) y sacar el signup público | `POST /users` público permite enumerar teléfonos vía 409 |
| Alta | Rate limiting por usuario e IP | Protección básica de la API |
| Media | Job de retención (pronósticos y estados de días pasados, claves vencidas, historial viejo) | Tamaño de la base acotado |
| Media | Métricas (Prometheus/OpenTelemetry): duración del ciclo, cola pendiente, tasa de `failed`, edad del pronóstico más fresco | Hoy la observabilidad son logs `key=value` y `/health` |
| Media | Particionar la evaluación por rango de campos | Escalar la evaluación horizontalmente |
| Media | Tope de rango en `GET /fields/{id}/forecasts` | Consultas acotadas |
| Baja | Grilla de ubicaciones compartida para pronósticos | Evitar duplicados entre campos vecinos |
| Baja | Enmascarado de teléfonos genérico (cualquier secuencia de ≥ 8 dígitos) y HMAC en los logs de idempotencia | Hoy está optimizado para números argentinos |
| Baja | Excepción del silencio para alertas críticas (opt-in del usuario) | Hoy el silencio no tiene excepciones |
| Baja | Deploy (contenedores en ECS/Cloud Run + Postgres administrado, migraciones como job previo) | Alcance actual: solo CI |

---

## CI, GitHub Pages y proceso

**CI** ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) en cada PR y push a `main`:
ruff, `ruff format --check`, mypy strict, contrato OpenAPI, la suite completa contra un Postgres 16
de servicio con cobertura mínima de 90%, y `docker build`. Permisos mínimos, acciones pinneadas por
SHA (Dependabot las mantiene al día) y sin secretos. `main` exige los checks en verde para mergear.

**GitHub Pages** (en cada push a `main`): Swagger UI estático con el contrato versionado y el
reporte HTML de cobertura → <https://nicolasssosa.github.io/agrobot-weather-alerts/>.

**Proceso:** el proyecto se construyó con un equipo de agentes de IA con roles separados (PM,
coder, QA, code review y security review), una historia por PR, merge gate por criticidad y
máximo 3 rondas de fixes por historia. Los roles están en [`.agents/`](.agents/README.md) y el
proceso en [`docs/PROCESS.md`](docs/PROCESS.md); cada PR incluye los reportes de QA y de las reviews.

---

## Documentación

| Doc | Contenido |
|-----|-----------|
| [REQUIREMENTS](docs/REQUIREMENTS.md) | Alcance, RF/RNF, **decisiones de producto** y supuestos |
| [BACKLOG](docs/BACKLOG.md) | Historias con criterios de aceptación y follow-ups de las reviews |
| [DATA_MODEL](docs/DATA_MODEL.md) | Esquema, constraints, índices y migraciones |
| [API](docs/API.md) · [openapi.json](docs/openapi.json) | Endpoints, convenciones y contrato |
| [JOBS](docs/JOBS.md) | Evaluador, episodios, dispatcher, digest, horario de silencio, worker, ingesta mock y seed |
| [TESTING](docs/TESTING.md) | Estrategia y casos de test |
| [PROCESS](docs/PROCESS.md) · [.agents](.agents/README.md) | Proceso SDLC y equipo de agentes |
