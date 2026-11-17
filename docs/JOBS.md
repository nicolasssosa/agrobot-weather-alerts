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
async def evaluate_alerts(
    session: AsyncSession, *, now: datetime, horizon_days: int, max_forecast_age_hours: int,
    future_tolerance_minutes: int,
) -> int
```

Una sola sentencia set-based (nada de traer filas a Python y loopear):

```sql
INSERT INTO notifications
    (alert_id, forecast_date, event_type, probability, threshold, status, next_attempt_at)
SELECT a.id, wf.forecast_date, wf.event_type, wf.probability, a.threshold, 'pending', now()
FROM alerts a
JOIN fields f ON f.id = a.field_id   -- zona horaria del campo (S12a)
JOIN weather_forecasts wf
  ON wf.field_id = a.field_id
 AND wf.event_type = a.event_type
WHERE a.is_active AND a.deleted_at IS NULL   -- predicado igual al del índice parcial ix_alerts_active_field_event (S9)
  AND wf.forecast_date >= (:now)::date - 1          -- prefiltro con valor fijo: permite usar el índice (S12a CR-2)
  AND f.timezone IN (SELECT name FROM pg_timezone_names)   -- saltea campos con zona desconocida
  AND wf.forecast_date >= local_today                       -- "hoy" LOCAL del campo (S12a, D9)
  AND (:horizon_days = 0
       OR wf.forecast_date < local_today + :horizon_days)    -- horizonte (D2)
  AND wf.probability   >= a.threshold                 -- "a partir de" => >=
ON CONFLICT (alert_id, forecast_date) DO NOTHING
RETURNING id;
```

donde `local_today` es la expresión protegida:

```sql
CASE WHEN f.timezone IN (SELECT name FROM pg_timezone_names)
     THEN (:now AT TIME ZONE f.timezone)::date
END
```

El `IN` del `WHERE` solo no alcanza: Postgres no garantiza en qué orden evalúa las condiciones y
puede calcular `AT TIME ZONE` (que lanza error con una zona desconocida) antes de descartar la fila.
El `CASE` sí garantiza el orden por fila: con una zona desconocida da `NULL`, la comparación da
falso y el resto de los campos se evalúa igual. Una zona desconocida nunca aborta el ciclo.

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
3. Fechas pasadas respecto del **"hoy" local del campo** no disparan; se evalúan los días en
   `[hoy_local, hoy_local + ALERT_HORIZON_DAYS)` (default 7; `0` = sin límite). Ejemplo: a las 22:00
   de Buenos Aires (01:00 UTC del día siguiente) "hoy" sigue siendo el día local.
4. Solo matchea mismo campo y mismo `event_type`.
5. Correr dos veces no duplica (`UNIQUE (alert_id, forecast_date)`).
6. Si la probabilidad baja y vuelve a subir para el mismo día, **no** se re-notifica (limitación
   documentada en README).
7. **Pronósticos desactualizados no disparan** (S11, D14): solo se evalúan pronósticos con
   `issued_at >= now - max_forecast_age_hours` (`MAX_FORECAST_AGE_HOURS`, default 24). Si para
   alertas activas hay pronósticos en la ventana que quedaron afuera por viejos, se loguea un
   **warning** `stale_forecasts count=<n> fields=<m> max_age_hours=<h>` (señal de que la ingesta no
   está trayendo datos frescos). Se cuentan con una consulta agregada aparte (sin traer filas):
   `count` = pronósticos **distintos** que habrían disparado alguna alerta activa
   (`probability >= threshold`) de no ser viejos; `fields` = campos distintos.
   Defensa adicional: tampoco se evalúan pronósticos con `issued_at > now + FORECAST_FUTURE_TOLERANCE_MINUTES`.

`now` lo calcula el worker (`datetime.now(UTC)`) y se inyecta (testeable); "hoy" se deriva por campo en SQL.
Además del warning de viejos, si se excluyen pronósticos con `issued_at` adelantado se loguea
`future_forecasts count=<n> fields=<m> tolerance_minutes=<t>` (S12a). Ambos warnings salen de **una sola**
consulta agregada con `FILTER`. Si hay campos con alertas activas cuya zona Postgres no reconoce, se
saltean y se loguea `invalid_timezone_fields count=<n>`.

## Dispatcher — `app/jobs/dispatcher.py` (patrón lease)

```python
async def dispatch_pending(
    sessionmaker: async_sessionmaker[AsyncSession], notifier: Notifier, *, now: datetime,
    batch_size: int, max_attempts: int, backoff_base_seconds: int, timeout_seconds: float,
    concurrency: int, lease_seconds: int,
) -> DispatchResult
```

**Regla de oro: nunca se espera a la red con una transacción abierta.** (S6 CR-1/SEC-2: con el
envío dentro de la transacción del claim, un proveedor colgado dejaba ~17 min de locks, frenaba el
autovacuum de toda la base y bloqueaba DELETEs de la API.)

Algoritmo, en **dos transacciones cortas** con el envío en el medio:

1. **Claim + lease + validación** (transacción 1, milisegundos). Además del lease, el claim
   **re-verifica contra la alerta actual** (S9, D12) **y contra la frescura del pronóstico** (S11, D14):
   si el pronóstico vigente de ese (campo, día, evento) tiene `issued_at < now - MAX_FORECAST_AGE_HOURS`
   se cancela con `last_error = stale_forecast`; además, si la alerta está borrada (`deleted_at`),
   inactiva, o `n.probability < a.threshold` (umbral vigente), la notificación pasa a
   `status='cancelled'`, `last_error` = `alert_deleted` | `alert_inactive` | `below_current_threshold`
   en la misma transacción y **no se envía**. Contarlas en `DispatchResult.cancelled`.
   En una sesión nueva del `sessionmaker`
   ```sql
   SELECT n.id, n.forecast_date, n.event_type, n.probability, n.threshold, n.attempts,
          a.is_active, a.deleted_at, a.threshold AS current_threshold,   -- validación D12 (S9)
          u.phone_number, f.name AS field_name
   FROM notifications n
   JOIN alerts a ON a.id = n.alert_id
   JOIN fields f ON f.id = a.field_id
   JOIN users  u ON u.id = f.user_id
   WHERE n.status = 'pending' AND n.next_attempt_at <= :now
   ORDER BY n.next_attempt_at, n.id
   LIMIT :batch_size
   FOR UPDATE OF n SKIP LOCKED
   ```
   y en la misma transacción `UPDATE notifications SET next_attempt_at = :lease_until
   WHERE id IN (...)` con `lease_until = now + lease_seconds` → **commit** (libera los locks).
   **`now` debe ser la hora real del momento del claim** (S6 SEC-3): nunca reutilizar un `now` viejo
   entre lotes, o el lease nace vencido y otra réplica reenvía. `lease_until` funciona además como
   **token de propiedad** del lote. Las filas quedan `pending` pero "arrendadas":
   ningún otro worker las ve hasta que venza el lease. No requiere columnas nuevas.
   Se copian los datos a objetos planos (dataclass) antes de cerrar la sesión.
2. **Envío fuera de toda transacción**, en paralelo acotado (`asyncio.Semaphore(concurrency)` +
   `asyncio.gather`): `await asyncio.wait_for(notifier.send(msg), timeout=timeout_seconds)`.
   Cada envío produce un resultado (ok / error); una excepción (`Exception`, incluido
   `TimeoutError`) nunca corta el lote. `asyncio.CancelledError` se propaga (shutdown): las filas
   arrendadas se reintentan solas cuando vence el lease.
3. **Resultados** (transacción 2, milisegundos): en una sesión nueva, por cada notificación
   `UPDATE … WHERE id = :id AND status = 'pending' AND next_attempt_at = :lease_until` (solo el dueño
   del lease escribe: si el lease venció y otro worker la reclamó, o si la alerta se borró, afecta 0
   filas y se ignora; así nunca se cuenta dos veces un intento). `now` para `sent_at` y el backoff
   es la hora real al momento de registrar:
   - **Éxito:** `status='sent'`, `sent_at=now`, `attempts = attempts + 1`, `last_error=NULL`.
   - **Error:** `attempts = attempts + 1`, `last_error` = `f"{type(e).__name__}: {e}"` con el
     teléfono enmascarado (todas sus variantes) y truncado a 1000.
     - si `attempts + 1 >= max_attempts` → `status='failed'`;
     - si no → `next_attempt_at = now + backoff_base_seconds * 2 ** attempts` (con `attempts` previo
       al incremento: 30 s, 60 s, 120 s…).
   → **commit**.
4. Loguear `dispatch_done sent=.. retried=.. failed=.. cancelled=.. duration_ms=..`.

`lease_seconds` debe ser mayor que el peor caso de envío del lote
(`ceil(batch_size / concurrency) * timeout_seconds`); se valida en Settings.

Semántica de entrega: **at-least-once**. Si el proceso muere después de enviar y antes de la
transacción 2, la notificación se reenvía cuando vence el lease. Mitigable en producción con una
idempotency key (`notification_id`, ya incluida en `NotificationMessage`) en el proveedor.

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
async def run_cycle(
    sessionmaker, notifier, settings, *, now: datetime | None = None,
    clock: Callable[[], datetime] = utc_now, stop_event: asyncio.Event | None = None,
) -> CycleResult
```
1. `now = now or datetime.now(UTC)`.
2. `async with sessionmaker() as s:` → `created = await evaluate_alerts(s, today=now.date())` → `await s.commit()`.
3. Despachar en lotes hasta vaciar la cola vencida: llamar
   `dispatch_pending(sessionmaker, notifier, ...)` (maneja sus propias transacciones) mientras el
   lote haya venido lleno (`sent + retried + failed == batch_size`), **con un `now` fresco
   (`datetime.now(UTC)`) en cada llamada** (S6 SEC-3). Acumular el `DispatchResult`.
   **Si `stop_event` está seteado, no arrancar un lote nuevo** (S7 SEC-2): el shutdown no espera
   a drenar toda la cola; lo pendiente queda para el próximo arranque.
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

`ingest_forecasts(session, provider, start=, days=, source="mock", field_ids=None) -> int`
(`field_ids` opcional: si se pasa, solo ingesta esos campos):
- Itera todos los `fields` ordenados por id, de a 500 (`.limit(500)` + `where id > last_id`).
- Cada `ForecastPoint` trae `issued_at` (cuándo emitió el proveedor). El mock usa la hora de la
  corrida del modelo: `now` UTC truncado a la hora.
- **Validación de `issued_at` antes del upsert** (un dato inválido nunca aborta el lote):
  - `None` o sin zona horaria → el punto se descarta; `logger.warning("forecast_invalid_issued_at count=%d")`.
  - En el futuro más allá de `FORECAST_FUTURE_TOLERANCE_MINUTES` (default 60) → se **acota a `now`**
    (así un dato real posterior lo puede pisar); `logger.warning("forecast_issued_in_future count=%d")`.
  - Se normaliza a UTC.
- Upsert **condicional** (S11, D14): `postgresql.insert(...).on_conflict_do_update(
  index_elements=[field_id, forecast_date, event_type], set_={probability, source, issued_at,
  fetched_at: func.now(), updated_at: func.now()}, where=WeatherForecast.issued_at < excluded.issued_at)`.
  Un dato **igual o más viejo** que el guardado no lo pisa (lotes atrasados, reintentos).
- Devuelve las filas **escritas** (insertadas o actualizadas); las descartadas por viejas se loguean
  como `forecasts_skipped_older count=<n>`. No commitea.

`python -m app.scripts.ingest [--days 14]`: ingesta desde `datetime.now(UTC).date()` y commit.

## Seed — `app/scripts/seed.py`

Idempotente (usar upserts `ON CONFLICT DO NOTHING` / buscar antes de crear):
- 2 usuarios: `+5491100000001 "Ana Gómez"`, `+5491100000002 "Bruno Díaz"`.
- Ana: campos `Lote Norte (-34.60, -58.38)` y `Lote Sur (-34.90, -58.50)`. Bruno: `La Esperanza (-31.42, -64.18)`.
- Alertas: Ana/Lote Norte `frost ≥ 60`, Ana/Lote Norte `rain ≥ 80`, Ana/Lote Sur `hail ≥ 50`,
  Bruno/La Esperanza `heat_wave ≥ 40`.
- Corre la ingesta mock para 14 días **solo sobre los 3 campos del seed** (`ingest_forecasts(...,
  field_ids=[...])`): nunca pisa pronósticos de otros campos (S7 SEC-1). Commit. Imprime los IDs
  creados y un ejemplo de curl con la URL completa (`http://localhost:8000/api/v1/...`).
- **Guarda de entorno:** si `Settings.environment` es `staging` o `production`, sale con error sin
  tocar la base (S7 SEC-1).
- No es seguro ante dos corridas concurrentes (check-then-insert): es un script de demo one-shot.
