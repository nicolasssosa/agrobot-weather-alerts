# Background jobs

```
          (job existente, mockeado)                         este repo
 ┌──────────────────────────┐        ┌──────────────────────────────────────────────┐
 │ ingesta  ──upsert──▶ weather_forecasts ──▶ Evaluador ──INSERT──▶ notifications    │
 └──────────────────────────┘        │                     (pending, outbox)        │
                                     │                          │                   │
                                     │                     Dispatcher ──▶ Notifier  │
                                     │                 (SKIP LOCKED, retries)  (log)│
                                     └──────────────────────────────────────────────┘
```

Un solo proceso `worker` corre en loop: **evaluar → despachar → dormir**. Ambos pasos son
idempotentes y seguros con varias réplicas del worker en paralelo.

## Evaluador — `app/jobs/evaluator.py`

```python
async def evaluate_alerts(session: AsyncSession, *, today: date) -> int
```

Una sola sentencia set-based (nada de traer filas a Python y loopear):

```sql
INSERT INTO notifications
    (alert_id, forecast_date, event_type, probability, threshold, status, next_attempt_at)
SELECT a.id, wf.forecast_date, wf.event_type, wf.probability, a.threshold, 'pending', now()
FROM alerts a
JOIN weather_forecasts wf
  ON wf.field_id = a.field_id
 AND wf.event_type = a.event_type
WHERE a.is_active            -- literal así (no `IS true`): habilita el índice parcial ix_alerts_active_field_event
  AND wf.forecast_date >= :today                      -- todos los días futuros (incluye hoy)
  AND wf.probability   >= a.threshold                 -- "a partir de" => >=
ON CONFLICT (alert_id, forecast_date) DO NOTHING
RETURNING id;
```

Implementación: con SQLAlchemy Core
(`sqlalchemy.dialects.postgresql.insert(Notification).from_select([...], select_stmt)
.on_conflict_do_nothing(index_elements=["alert_id", "forecast_date"]).returning(Notification.id)`),
Contar **sin materializar** las filas: envolver el insert en un CTE y contar en SQL
(`WITH ins AS (INSERT … RETURNING 1) SELECT count(*) FROM ins`; en SQLAlchemy
`select(func.count()).select_from(insert_stmt.cte("ins"))`). Sigue siendo una sola sentencia. Nunca
`len(result.all())`: con un backlog grande trae cientos de MB a memoria (S5 CR-2). **No commitea.** Loguear `alerts_evaluated created=<n>`.

Reglas de negocio (cubiertas por tests):
1. `probability >= threshold` dispara; `probability < threshold` no.
2. Alertas inactivas no disparan.
3. Fechas pasadas (`< today`) no disparan; **todas** las futuras sí (el enunciado no define horizonte).
4. Solo matchea mismo campo y mismo `event_type`.
5. Correr dos veces no duplica (`UNIQUE (alert_id, forecast_date)`).
6. Si la probabilidad baja y vuelve a subir para el mismo día, **no** se re-notifica (limitación
   documentada en README).

`today` lo calcula el worker como `datetime.now(UTC).date()` y se inyecta (testeable).

## Dispatcher — `app/jobs/dispatcher.py`

```python
async def dispatch_pending(
    session, notifier, *, now: datetime, batch_size: int, max_attempts: int,
    backoff_base_seconds: int, timeout_seconds: float,
) -> DispatchResult
```

Algoritmo:

1. Reclamar un lote (lockeando **solo** filas de `notifications`):
   ```sql
   SELECT n.*, u.phone_number, f.name AS field_name
   FROM notifications n
   JOIN alerts a ON a.id = n.alert_id
   JOIN fields f ON f.id = a.field_id
   JOIN users  u ON u.id = f.user_id
   WHERE n.status = 'pending' AND n.next_attempt_at <= :now
   ORDER BY n.next_attempt_at, n.id
   LIMIT :batch_size
   FOR UPDATE OF n SKIP LOCKED
   ```
   En SQLAlchemy: `.with_for_update(of=Notification, skip_locked=True)`.
   `SKIP LOCKED` ⇒ dos workers nunca envían la misma notificación.
2. Para cada fila:
   - `text = build_message_text(...)`; `msg = NotificationMessage(n.id, phone_number, text)`.
   - `await asyncio.wait_for(notifier.send(msg), timeout=timeout_seconds)`.
   - **Éxito:** `status='sent'`, `sent_at=now`, `attempts += 1`, `last_error=None`.
   - **Excepción** (cualquiera, incluido `TimeoutError`; nunca dejar que corte el lote):
     `attempts += 1`, `last_error = f"{type(e).__name__}: {e}"[:1000]`.
     - si `attempts >= max_attempts` → `status='failed'` (contar como `failed`).
     - si no → `next_attempt_at = now + timedelta(seconds=backoff_base_seconds * 2 ** (attempts - 1))`
       (contar como `retried`). Ej. base 30: 30s, 60s, 120s, 240s…
3. `await session.flush()`; **no commitea** (el worker commitea el lote → libera locks).
4. Loguear `dispatch_done sent=.. retried=.. failed=..`.

Semántica de entrega: **at-least-once**. Si el proceso muere después de enviar y antes del commit,
la notificación se reenvía en el próximo ciclo. Aceptable para un aviso climático; mitigable en
producción con una idempotency key (`notification_id`) en el proveedor de WhatsApp.

### Texto del mensaje

```python
def build_message_text(*, event_type, field_name, probability, threshold, forecast_date) -> str
```
Formato exacto (tests lo verifican):
```
⚠️ Alerta de {event_type.label_es} en "{field_name}": {probability}% de probabilidad para el {dd/mm/yyyy} (tu umbral: {threshold}%).
```
Ej: `⚠️ Alerta de helada en "Lote Norte": 85% de probabilidad para el 18/11/2026 (tu umbral: 70%).`

## Worker — `app/jobs/worker.py`

```python
async def run_cycle(sessionmaker, notifier, settings, *, now: datetime | None = None) -> CycleResult
```
1. `now = now or datetime.now(UTC)`.
2. `async with sessionmaker() as s:` → `created = await evaluate_alerts(s, today=now.date())` → `await s.commit()`.
3. Despachar en lotes hasta vaciar la cola vencida: repetir en sesión nueva
   `dispatch_pending(... batch_size=settings.dispatch_batch_size ...)` + `commit()` mientras el lote
   haya venido lleno (`sent + retried + failed == batch_size`). Acumular el `DispatchResult`.
4. Devolver `CycleResult(created, dispatch)`.

```python
async def run_forever(sessionmaker, notifier, settings, stop_event: asyncio.Event) -> None
```
- Loop hasta `stop_event.is_set()`: `run_cycle(...)`; si lanza excepción → `logger.exception(...)`
  y **seguir** (un error transitorio de DB no debe matar el worker).
- Espera interrumpible: `await asyncio.wait_for(stop_event.wait(), timeout=settings.worker_interval_seconds)`
  capturando `TimeoutError` (así SIGTERM corta la espera al instante).

```python
def main(argv: Sequence[str] | None = None) -> None
```
- `argparse`: flag `--once` (un ciclo y salir).
- `configure_logging()`, `settings = get_settings()`, `notifier = LoggingNotifier()`.
- Registrar `SIGINT`/`SIGTERM` con `loop.add_signal_handler(sig, stop_event.set)` → **graceful shutdown**
  (termina el ciclo en curso, no corta un lote a la mitad).
- Al salir: `await get_engine().dispose()`.
- `asyncio.run(...)`.

### ¿Por qué un proceso aparte y no `BackgroundTasks`/lifespan de FastAPI?
- Escala independiente de la API (N réplicas de API ≠ N evaluadores).
- Un crash/deploy de la API no corta envíos y viceversa.
- Sin dependencias extra (Celery/Redis/APScheduler): Postgres ya provee cola (`SKIP LOCKED`) e
  idempotencia (`ON CONFLICT`). Ver README §Decisiones.

## Ingesta mock — `app/weather/`

Simula el job existente. `MockWeatherProvider.fetch(field, start=, days=)` devuelve, para cada día
en `[start, start+days)` y **cada** `EventType`, una probabilidad determinística:
`random.Random(f"{seed}:{field.id}:{day.isoformat()}:{event.value}").randint(0, 100)`.

`ingest_forecasts(session, provider, start=, days=, source="mock") -> int`:
- Itera todos los `fields` ordenados por id, de a 500 (`.limit(500)` + `where id > last_id`).
- Upsert con `postgresql.insert(...).on_conflict_do_update(index_elements=[field_id, forecast_date, event_type], set_={probability, source, fetched_at: func.now(), updated_at: func.now()})`.
- Devuelve filas procesadas. No commitea.

`python -m app.scripts.ingest [--days 14]`: ingesta desde `datetime.now(UTC).date()` y commit.

## Seed — `app/scripts/seed.py`

Idempotente (usar upserts `ON CONFLICT DO NOTHING` / buscar antes de crear):
- 2 usuarios: `+5491100000001 "Ana Gómez"`, `+5491100000002 "Bruno Díaz"`.
- Ana: campos `Lote Norte (-34.60, -58.38)` y `Lote Sur (-34.90, -58.50)`. Bruno: `La Esperanza (-31.42, -64.18)`.
- Alertas: Ana/Lote Norte `frost ≥ 60`, Ana/Lote Norte `rain ≥ 80`, Ana/Lote Sur `hail ≥ 50`,
  Bruno/La Esperanza `heat_wave ≥ 40`.
- Corre la ingesta mock para 14 días. Commit. Imprime los IDs creados y un ejemplo de curl.
