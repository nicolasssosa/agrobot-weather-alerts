# Testing

## Cómo corren

- Postgres real (no SQLite: usamos `ON CONFLICT`, `FOR UPDATE SKIP LOCKED`, índices parciales).
  `make db-up` levanta la base; `agrobot_test` se crea en el init del contenedor.
  > Si el volumen ya existía sin esa base: `docker compose exec db psql -U postgres -c "CREATE DATABASE agrobot_test"`.
- `tests/conftest.py` (ya implementado, no modificar salvo para agregar factories):
  - aplica `alembic downgrade base` + `upgrade head` una vez por sesión → **las migraciones se testean**;
  - fixture `db_session`: transacción por test con rollback al final;
  - fixture `client`: `httpx.AsyncClient` contra la app con la sesión inyectada. **Agrega
    automáticamente un `Idempotency-Key` único (UUID v4) a todo `POST /api/v1/alerts` que no lo
    traiga** (desde S11b el header es obligatorio y la mayoría de los tests no prueban eso);
  - fixture `raw_client`: igual que `client` pero **sin** la clave automática; usarla para probar
    el header ausente o inválido;
  - `FakeNotifier(fail_times=n)` + fixture `fake_notifier`.
- `asyncio_mode = "auto"`: los tests son `async def test_...` sin decorador.
- Para jobs, llamar a las funciones con `db_session` directamente y pasar `today`/`now` fijos
  (nunca depender del reloj real).

## Factories (T11) — agregar a `tests/conftest.py`

Fixtures que devuelven funciones async; cada una hace `session.add(obj)` + `await session.flush()` y
devuelve el objeto. Valores por defecto únicos con un contador (`itertools.count()`).

```python
@pytest.fixture
def make_user(db_session):
    async def _make(**kw) -> User:
        n = next(_seq)
        user = User(
            phone_number=kw.get("phone_number", f"+54911{n:08d}"), name=kw.get("name", f"User {n}")
        )
        db_session.add(user)
        await db_session.flush()
        return user

    return _make
```
Análogamente:
- `make_field(user=None, **kw)` → crea user si no se pasa; `name=f"Field {n}"`, lat `-34.6`, lon `-58.4`.
- `make_forecast(field, *, forecast_date, event_type, probability, source="mock")` → `fetched_at=now(UTC)`.
- `make_alert(field, *, event_type=EventType.FROST, threshold=70, is_active=True)`.
- Helper `auth(user) -> dict[str, str]` que devuelve `{"X-User-Id": str(user.id)}`.

## Casos mínimos requeridos

### `tests/test_migrations.py`
- `alembic check` no detecta diferencias entre modelos y la base migrada
  (usar `alembic.command.check` con el `Config` apuntando a `TEST_DATABASE_URL`; correrlo en un
  test **sync** porque `env.py` usa `asyncio.run`).

### `tests/api/test_health.py`
- `GET /health` → 200 `{"status": "ok"}`.

### `tests/api/test_users.py`
- crea usuario → 201; teléfono duplicado → 409; teléfono inválido → 422.
- `GET /users/me` sin header → 401; con header de user inexistente → 401; ok → 200.

- `PATCH /users/me` (S13): setea y desactiva `quiet_hours`; omitir la clave no la cambia; `{}`,
  `start == end`, `"22:00:30"`, zona inválida o campo extra → 422; sin header → 401.

### `tests/api/test_fields.py`
- crear → 201 y aparece en el listado; nombre duplicado (mismo user) → 409;
  mismo nombre para otro user → 201; latitud 91 → 422.
- `GET /fields/{id}` de otro usuario → 404.
- límite (S14a): con `MAX_FIELDS_PER_USER` campos el siguiente → 409 `Field limit reached for this user`
  (también si el nombre está duplicado); otro usuario no se ve afectado; dos creaciones concurrentes
  en conexiones separadas con un solo lugar libre → una 201 y una 409.

### `tests/api/test_limits.py` (S14a)
- body de `MAX_REQUEST_BODY_BYTES + 1` → 413 con el detail exacto; exactamente el límite → no es 413;
  body chunked (sin `Content-Length`) mayor al límite → 413.
- payload con más de 20 errores de validación → 422 con 21 elementos y el último `too_many_errors`
  con la cantidad omitida correcta; con 20 o menos → sin elemento extra.
- lock retenido desde otra conexión + `lock_timeout` corto → 503, detail exacto y `Retry-After: 1`.

### `tests/core/test_db.py` (S14a)
- una conexión de `build_engine(settings)` muestra `statement_timeout` y `lock_timeout` configurados;
  con 0 → sin límite (`0`).

### `tests/api/test_forecasts.py`
- devuelve forecasts del campo ordenados por fecha; filtra por `event_type` y rango;
  `date_from > date_to` → 422; campo ajeno → 404.

### `tests/api/test_alerts.py`
- crear → 201 (`is_active` true); duplicado (campo, evento) → 409; campo ajeno → 404;
  `threshold` 0 y 101 → 422; `event_type` inválido → 422; campo extra → 422.
- listado con filtros (`field_id`, `event_type`, `is_active`) y paginación (`limit`/`offset`).
- PATCH threshold / is_active → 200; PATCH `{}` → 422; PATCH alerta ajena → 404.
- DELETE → 204 y luego GET → 404; DELETE borra en cascada sus notificaciones.

### `tests/api/test_notifications.py`
- solo lista notificaciones del usuario autenticado; filtro por `status` y `alert_id`.

### `tests/jobs/test_evaluator.py`  (el más importante)
- probabilidad == umbral → crea notificación (snapshot de probability/threshold/event_type correcto).
- probabilidad < umbral → no crea.
- alerta inactiva → no crea.
- fecha de ayer → no crea; fecha de hoy → crea; fecha lejana (hoy + 30) → crea.
- otro `event_type` u otro campo → no crea.
- correr dos veces → segunda vez devuelve 0 y hay una sola fila.
- varias alertas/días → cuenta correcta.
- episodios (S12b): arriba → abajo de `rearm_level` → arriba crea el episodio 2; quedarse arriba no
  repite; bajar dentro de la banda de histéresis y volver no repite; pronóstico viejo no re-arma.
- lock tomado por otra conexión más que `EVALUATOR_LOCK_WAIT_MS` (S14a) → devuelve 0, no crea ni
  re-arma nada y loguea `evaluation_skipped reason=lock_busy` (warning); el worker igual despacha
  en ese ciclo; después del skip la sesión sigue usable y el `lock_timeout` vuelve al de la conexión.
- lock tomado y liberado antes de vencer la espera → evalúa normalmente.
- con el evaluador encolado, un `lock_shared` nuevo (PATCH) espera detrás de él (no lo adelanta).
- PATCH de una alerta inexistente o ajena → 404 sin tomar el lock compartido.
- worker: la evaluación lanza → `evaluation_failed`, y el ciclo igual despacha.
- equivalencia SQL vs. `app/jobs/episodes.step` en secuencias generadas con semilla fija
  (`random.Random(<n>)`, sin dependencias nuevas).

### `tests/jobs/test_dispatcher.py`
- pendiente → `sent`, `sent_at` seteado, `attempts == 1`, el FakeNotifier recibió el texto exacto y
  el teléfono del dueño.
- notifier falla una vez → sigue `pending`, `attempts == 1`, `last_error` seteado,
  `next_attempt_at == now + backoff_base`; con `now` posterior se envía.
- falla `max_attempts` veces → `failed`.
- `next_attempt_at` en el futuro → no se toca.
- notifier lento (`asyncio.sleep` > timeout) → cuenta como fallo (no cuelga).
- respeta `batch_size`.
- `build_message_text` → string exacto de docs/JOBS.md.

- envío agrupado (S13): dos notificaciones vencidas del mismo usuario → un solo `send` con el texto
  de `build_digest_text` y ambas `sent`; usuarios distintos → un mensaje cada uno; más de
  `digest_max_items` → el excedente queda `pending` y vencido; fallo del envío → cada una con su
  propio backoff / `failed`.
- horario de silencio (S13): usuario en silencio → `deferred`, `next_attempt_at` = fin del silencio,
  `attempts` sin cambios, ningún `send`; con `now` = fin del silencio → se envían en un mensaje.
  Alerta borrada durante el silencio → `cancelled`. Día ya pasado en la zona del campo →
  `cancelled` con `forecast_date_passed`.
- `build_digest_text` → string exacto de docs/JOBS.md.

### `tests/jobs/test_quiet_hours.py` (S13)
- tabla de casos de `quiet_until` (BACKLOG.md §S13), con instantes UTC fijos.

### `tests/jobs/test_worker.py`
- `run_cycle` end-to-end con forecasts + alertas → notificaciones `sent`.
  Como `run_cycle` usa un `sessionmaker`, pasar uno ligado a la conexión del test:
  `async_sessionmaker(bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")`.
- `run_forever` con `stop_event` seteado tras el primer ciclo termina sin error;
  si `run_cycle` lanza, el loop continúa (monkeypatch).

### `tests/weather/test_ingestion.py`
- `MockWeatherProvider` es determinístico y devuelve `days × len(EventType)` puntos en 0..100.
- `ingest_forecasts` inserta y, al re-ejecutarse con otra semilla, actualiza (upsert) sin duplicar.

### Concurrencia (opcional, suma puntos)
- Dos `dispatch_pending` concurrentes en **conexiones distintas** (no usar `db_session`; crear
  engine propio y limpiar al final) no envían la misma notificación dos veces.
