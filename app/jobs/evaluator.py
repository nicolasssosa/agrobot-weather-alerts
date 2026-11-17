"""Evaluación de alertas vs. pronósticos almacenados.

Spec: docs/JOBS.md §Evaluador y §Episodios. Todo set-based: los candidatos (alerta por
pronóstico en la ventana) se calculan una vez como CTE y sobre ellos se aplican el
re-armado y el disparo, contando siempre en SQL (`SELECT count(*)`), nunca trayendo filas a
Python. No commitea: el llamador es dueño de la transacción.

Desde S12b (D1) la evaluación es **por episodio**: `alert_day_states` guarda, por
(alerta, día), el último episodio disparado y si el día está armado. Un día armado que
llega al umbral dispara el episodio siguiente; un día disparado se re-arma cuando la
probabilidad baja de `max(threshold - REARM_MARGIN, 1)`; entre esos dos valores está la
banda de histéresis. `UNIQUE (alert_id, forecast_date, episode)` es la red de seguridad de
idempotencia. Antes de evaluar se intenta `pg_try_advisory_xact_lock(EVALUATOR_LOCK_KEY)`: una
sola réplica evalúa por vez, así dos snapshots distintos no re-arman y disparan el mismo día
(episodio duplicado). Si el lock está tomado (otra réplica evaluando o un PATCH de alerta con
el lock compartido abierto) el ciclo **no espera**: no evalúa, devuelve 0 y loguea
`evaluation_skipped reason=lock_busy` (info), así el worker sigue al despacho del mismo ciclo.

Desde S12a (D9) el "hoy" es **local de cada campo**: `(:now AT TIME ZONE fields.timezone)::date`,
con `now` un `TIMESTAMPTZ` en UTC. Se evalúa `[hoy_local, hoy_local + horizon_days)`; el
horizonte (`ALERT_HORIZON_DAYS`, D2) de 0 significa sin límite.

El bound local (`AT TIME ZONE`) no es un valor constante, así que por sí solo no puede usarse
como condición de índice sobre `UNIQUE (field_id, forecast_date, event_type)`. Por eso se suma
un **prefiltro constante** (S12a CR-2) derivado de `now` en UTC: `forecast_date >= hoy_utc - 1`
y, con horizonte, `forecast_date <= hoy_utc + horizon_days + 1`. Es un superconjunto holgado
(los offsets del mundo están entre -12 y +14 h) y los bounds locales exactos siguen filtrando.

Desde S11 (D14) sólo se evalúan pronósticos frescos: `issued_at >= now - MAX_FORECAST_AGE_HOURS`.
Además, un `issued_at` en el futuro más allá de `FORECAST_FUTURE_TOLERANCE_MINUTES` tampoco se
evalúa (defensa ante datos inválidos que la ingesta debió acotar). Los pronósticos que quedaron
afuera por viejos o por adelantados se cuentan con **una sola** consulta agregada (`FILTER`) y,
si hay alguno, se loguea un warning (señal de que la ingesta no trae datos frescos o emite
datos inválidos).

Desde S12a CR-1/SEC-1 el evaluador sólo considera campos cuya zona existe en
`pg_timezone_names`: una zona desconocida haría fallar `AT TIME ZONE` y dejaría sin evaluar a
todos los campos. Además, la expresión del "hoy" local va envuelta en un `CASE` (CR-4/SEC-1)
porque el `IN` del `WHERE` no garantiza el orden de evaluación: el planner puede calcular
`AT TIME ZONE` antes de descartar la fila. Los campos salteados se cuentan y se loguea
`invalid_timezone_fields count=<n>`.
"""

import logging
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import (
    Boolean,
    ColumnElement,
    Date,
    DateTime,
    Integer,
    Select,
    and_,
    case,
    func,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnExpressionArgument
from sqlalchemy.sql.selectable import CTE

from app.models import (
    Alert,
    AlertDayState,
    Field,
    Notification,
    NotificationStatus,
    WeatherForecast,
)
from app.services.timezones import PG_TIMEZONE_NAMES

logger = logging.getLogger(__name__)

#: Clave del lock de evaluación (`pg_try_advisory_xact_lock`): una réplica evalúa por vez.
EVALUATOR_LOCK_KEY: int = 202611170008

#: Columnas del INSERT ... SELECT de notificaciones, en el mismo orden que el `select`.
_INSERT_COLUMNS = (
    "alert_id",
    "forecast_date",
    "episode",
    "event_type",
    "probability",
    "threshold",
    "status",
    "next_attempt_at",
)


def _utc_today(now: datetime) -> date:
    """Fecha de `now` en UTC: la base del prefiltro constante (S12a CR-2)."""
    return now.astimezone(UTC).date()


def _local_today(now: datetime) -> ColumnElement[date]:
    """Expresión SQL del "hoy" local del campo, protegida con `CASE` (S12a CR-4/SEC-1).

    `CASE WHEN fields.timezone IN (SELECT name FROM pg_timezone_names)
          THEN (:now AT TIME ZONE fields.timezone)::date END`

    El `IN` del `WHERE` no alcanza: Postgres no garantiza el orden de evaluación y puede
    calcular `AT TIME ZONE` antes de descartar la fila (por ejemplo como filtro de un hash
    join), y una zona desconocida aborta la sentencia entera. El `CASE` sí impone el orden
    por fila: con una zona desconocida el resultado es `NULL` (el `ELSE` implícito), la
    comparación da falso y el resto de los campos se evalúa igual.
    """
    return case(
        (
            _known_timezone(),
            literal(now, type_=DateTime(timezone=True))
            .op("AT TIME ZONE")(Field.timezone)
            .cast(Date),
        )
    )


def _started_window(
    now: datetime, horizon_days: int
) -> tuple[ColumnElement[date], ColumnElement[bool]]:
    """Condiciones de la ventana de días evaluada, relativas al "hoy" local del campo.

    `horizon_days = 0` (D2) significa sin límite superior: solo cuenta `forecast_date >= hoy`.
    """
    today = _local_today(now)
    return today, or_(
        literal(horizon_days, Integer) == 0,
        WeatherForecast.forecast_date < today + literal(horizon_days, Integer),
    )


def _freshness_cutoff(*, now: datetime, max_forecast_age_hours: int) -> datetime:
    """Instante mínimo de emisión aceptado: `now - max_forecast_age_hours`."""
    return now - timedelta(hours=max_forecast_age_hours)


def _future_limit(*, now: datetime, future_tolerance_minutes: int) -> datetime:
    """Instante máximo de emisión aceptado: `now + future_tolerance_minutes`."""
    return now + timedelta(minutes=future_tolerance_minutes)


def _date_floor(now: datetime) -> ColumnElement[date]:
    """Prefiltro constante `forecast_date >= (:now)::date - 1` (S12a CR-2).

    La fecha se calcula en Python **en UTC** y se bindea como `DATE`: así el rango no depende
    de la zona horaria de la sesión de Postgres y es una condición de índice.
    """
    return literal(_utc_today(now) - timedelta(days=1), type_=Date())


def _date_ceiling(now: datetime, horizon_days: int) -> ColumnElement[date]:
    """Prefiltro constante `forecast_date <= (:now)::date + horizon_days + 1` (S12a CR-2).

    Sólo aplica con horizonte; es un superconjunto del bound local exacto.
    """
    return literal(_utc_today(now) + timedelta(days=horizon_days + 1), type_=Date())


def _known_timezone() -> ColumnElement[bool]:
    """`fields.timezone IN (SELECT name FROM pg_timezone_names)` (S12a CR-1/SEC-1)."""
    return Field.timezone.in_(select(PG_TIMEZONE_NAMES.c.name))


def _candidate_conditions(
    *, now: datetime, horizon_days: int
) -> list[ColumnExpressionArgument[bool]]:
    """Predicados de los candidatos (alerta por pronóstico), sin mirar la probabilidad.

    Alertas activas y no borradas (el predicado del índice parcial, S9), zona conocida por
    Postgres, prefiltro constante de fechas (CR-2) y ventana local exacta (D9/D2). La
    comparación contra el umbral la agrega cada paso: el re-armado usa `probability <
    rearm_level` y el disparo `probability >= threshold`.
    """
    today, in_horizon = _started_window(now, horizon_days)
    conditions: list[ColumnExpressionArgument[bool]] = [
        # Boolean bare (no `IS true`) para que el planner pueda usar el índice parcial.
        Alert.is_active,
        Alert.deleted_at.is_(None),
        _known_timezone(),
        WeatherForecast.forecast_date >= _date_floor(now),
        WeatherForecast.forecast_date >= today,
        in_horizon,
    ]
    if horizon_days > 0:
        conditions.append(WeatherForecast.forecast_date <= _date_ceiling(now, horizon_days))
    return conditions


def _alert_conditions(*, now: datetime, horizon_days: int) -> list[ColumnExpressionArgument[bool]]:
    """Predicados comunes a los candidatos y a los warnings (`probability >= threshold`).

    Los warnings de S11/S12a cuentan pronósticos que *habrían disparado* alguna alerta activa.
    """
    return [
        *_candidate_conditions(now=now, horizon_days=horizon_days),
        WeatherForecast.probability >= Alert.threshold,
    ]


def _build_candidates(
    *,
    now: datetime,
    horizon_days: int,
    max_forecast_age_hours: int,
    future_tolerance_minutes: int,
) -> CTE:
    """CTE `candidatos`: (alerta, día) de la ventana con su pronóstico vigente y el umbral.

    Mismas condiciones que el evaluador previo a S12b salvo `probability >= threshold`: el
    re-armado necesita ver también los días que bajaron del umbral.
    """
    return (
        select(
            Alert.id.label("alert_id"),
            WeatherForecast.forecast_date.label("forecast_date"),
            WeatherForecast.event_type.label("event_type"),
            WeatherForecast.probability.label("probability"),
            Alert.threshold.label("threshold"),
        )
        .select_from(Alert)
        .join(Field, Field.id == Alert.field_id)
        .join(
            WeatherForecast,
            (WeatherForecast.field_id == Alert.field_id)
            & (WeatherForecast.event_type == Alert.event_type),
        )
        .where(
            *_candidate_conditions(now=now, horizon_days=horizon_days),
            # Frescura (S11, D14): borde incluido, `== cutoff` se evalúa.
            WeatherForecast.issued_at
            >= _freshness_cutoff(now=now, max_forecast_age_hours=max_forecast_age_hours),
            # Defensa ante un `issued_at` futuro que la ingesta no acotó (S11 SEC-1).
            WeatherForecast.issued_at
            <= _future_limit(now=now, future_tolerance_minutes=future_tolerance_minutes),
        )
        .cte("candidatos")
    )


def _rearm_level(threshold: ColumnElement[int], rearm_margin: int) -> ColumnElement[int]:
    """`GREATEST(threshold - :rearm_margin, 1)` en SQL (docs/JOBS.md §Episodios)."""
    return func.greatest(threshold - literal(rearm_margin, Integer), literal(1, Integer))


def _build_rearm_statement(
    *,
    now: datetime,
    horizon_days: int,
    max_forecast_age_hours: int,
    future_tolerance_minutes: int,
    rearm_margin: int,
) -> Select[tuple[int]]:
    """Re-arma los días disparados cuya probabilidad bajó de `rearm_level`.

    `UPDATE ... RETURNING` envuelto en un CTE: el conteo lo hace Postgres, sin traer filas.
    """
    candidates = _build_candidates(
        now=now,
        horizon_days=horizon_days,
        max_forecast_age_hours=max_forecast_age_hours,
        future_tolerance_minutes=future_tolerance_minutes,
    )
    rearmed = (
        update(AlertDayState)
        .where(
            AlertDayState.alert_id == candidates.c.alert_id,
            AlertDayState.forecast_date == candidates.c.forecast_date,
            AlertDayState.armed.is_(False),
            candidates.c.probability < _rearm_level(candidates.c.threshold, rearm_margin),
        )
        .values(armed=True, updated_at=func.now())
        .returning(AlertDayState.id)
        .cte("rearmed")
    )
    return select(func.count()).select_from(rearmed)


def _build_statement(
    *,
    now: datetime,
    horizon_days: int,
    max_forecast_age_hours: int,
    future_tolerance_minutes: int,
) -> Select[tuple[int]]:
    """Dispara los episodios y crea sus notificaciones; cuenta en SQL.

    `WITH fired AS (INSERT INTO alert_day_states ... ON CONFLICT ... DO UPDATE
    WHERE alert_day_states.armed RETURNING ...), ins AS (INSERT INTO notifications ...
    ON CONFLICT (alert_id, forecast_date, episode) DO NOTHING RETURNING 1)
    SELECT count(*) FROM ins`.
    """
    candidates = _build_candidates(
        now=now,
        horizon_days=horizon_days,
        max_forecast_age_hours=max_forecast_age_hours,
        future_tolerance_minutes=future_tolerance_minutes,
    )
    fired = (
        pg_insert(AlertDayState)
        .from_select(
            ["alert_id", "forecast_date", "episode", "armed"],
            select(
                candidates.c.alert_id,
                candidates.c.forecast_date,
                literal(1, Integer),
                literal(False, Boolean),
            ).where(candidates.c.probability >= candidates.c.threshold),
            include_defaults=False,
        )
        .on_conflict_do_update(
            index_elements=["alert_id", "forecast_date"],
            set_={
                "episode": AlertDayState.episode + 1,
                "armed": False,
                "updated_at": func.now(),
            },
            # Un día que sigue arriba (`armed = false`) no vuelve a disparar.
            where=and_(AlertDayState.armed),
        )
        .returning(
            AlertDayState.alert_id,
            AlertDayState.forecast_date,
            AlertDayState.episode,
        )
        .cte("fired")
    )
    inserted = (
        pg_insert(Notification)
        .from_select(
            _INSERT_COLUMNS,
            select(
                fired.c.alert_id,
                fired.c.forecast_date,
                fired.c.episode,
                candidates.c.event_type,
                candidates.c.probability,
                candidates.c.threshold,
                literal(NotificationStatus.PENDING.value),
                func.now(),
            ).select_from(
                fired.join(
                    candidates,
                    and_(
                        fired.c.alert_id == candidates.c.alert_id,
                        fired.c.forecast_date == candidates.c.forecast_date,
                    ),
                )
            ),
            include_defaults=False,
        )
        .on_conflict_do_nothing(index_elements=["alert_id", "forecast_date", "episode"])
        .returning(Notification.id)
        .cte("ins")
    )
    return select(func.count()).select_from(inserted)


def _build_out_of_window_statement(
    *,
    now: datetime,
    horizon_days: int,
    max_forecast_age_hours: int,
    future_tolerance_minutes: int,
) -> Select[tuple[int, int, int, int]]:
    """Cuenta en **una** consulta los pronósticos descartados por viejos y por adelantados.

    Mismas condiciones que el INSERT (alertas activas y no borradas, zona conocida, prefiltro
    y ventana de días, `probability >= threshold`) salvo el corte de `issued_at`, que se aplica
    con agregados `FILTER (WHERE ...)`: `stale` = `issued_at < now - max_age`,
    `future` = `issued_at > now + tolerance`. Devuelve
    `(pronósticos viejos, campos viejos, pronósticos adelantados, campos adelantados)`.

    El `count` es de pronósticos **distintos**: el fan-out del JOIN con varias alertas del
    mismo (campo, evento) no lo infla (S11 QA-1/CR-3). Agregado en SQL: no se traen filas.
    """
    stale = WeatherForecast.issued_at < _freshness_cutoff(
        now=now, max_forecast_age_hours=max_forecast_age_hours
    )
    ahead = WeatherForecast.issued_at > _future_limit(
        now=now, future_tolerance_minutes=future_tolerance_minutes
    )
    return (
        select(
            func.count(func.distinct(WeatherForecast.id)).filter(stale),
            func.count(func.distinct(WeatherForecast.field_id)).filter(stale),
            func.count(func.distinct(WeatherForecast.id)).filter(ahead),
            func.count(func.distinct(WeatherForecast.field_id)).filter(ahead),
        )
        .select_from(Alert)
        .join(Field, Field.id == Alert.field_id)
        .join(
            WeatherForecast,
            (WeatherForecast.field_id == Alert.field_id)
            & (WeatherForecast.event_type == Alert.event_type),
        )
        .where(*_alert_conditions(now=now, horizon_days=horizon_days))
    )


def _build_invalid_timezone_statement() -> Select[tuple[int]]:
    """Cuenta los campos con alertas activas cuya zona Postgres no reconoce (S12a SEC-1)."""
    return (
        select(func.count(func.distinct(Field.id)))
        .select_from(Alert)
        .join(Field, Field.id == Alert.field_id)
        .where(Alert.is_active, Alert.deleted_at.is_(None), ~_known_timezone())
    )


async def evaluate_alerts(
    session: AsyncSession,
    *,
    now: datetime,
    horizon_days: int,
    max_forecast_age_hours: int,
    future_tolerance_minutes: int,
    rearm_margin: int,
) -> int:
    """Crea las notificaciones pendientes que disparan las alertas activas.

    "Hoy" es la fecha **local de cada campo** (`fields.timezone`, D9): con `now` a las
    01:00 UTC del 17, un campo en Buenos Aires sigue en el 16. Se evalúan los días
    `[hoy_local, hoy_local + horizon_days)`; `horizon_days = 0` (D2) no pone límite superior.

    Los campos cuya zona no existe en `pg_timezone_names` se saltean (una zona desconocida
    rompería `AT TIME ZONE` para todos) y se countan en `invalid_timezone_fields` (S12a SEC-1).

    Por episodio (D1, S12b): un día armado con `probability >= threshold` dispara el
    episodio `episode + 1`; un día disparado se re-arma cuando la probabilidad baja de
    `max(threshold - rearm_margin, 1)`; en la banda de histéresis no cambia nada. Todo en la
    misma transacción del llamador, bajo `pg_try_advisory_xact_lock(EVALUATOR_LOCK_KEY)` para
    que dos réplicas no generen episodios duplicados. `UNIQUE (alert_id, forecast_date,
    episode)` es la red de seguridad de idempotencia.

    Si el lock está tomado (otra réplica evaluando o un PATCH de alerta abierto con el lock
    compartido) no se espera: devuelve 0 sin tocar nada y loguea
    `evaluation_skipped reason=lock_busy` (info). La evaluación se hace en el ciclo siguiente
    y el worker sigue al despacho del mismo ciclo (S14a): con `DB_LOCK_TIMEOUT_MS` /
    `DB_STATEMENT_TIMEOUT_MS` una espera terminaría en error y cortaría también el despacho.

    Matchea por campo y tipo de evento y solo pronósticos dentro de la ventana de frescura
    (`issued_at >= now - max_forecast_age_hours`, borde incluido). Un `issued_at` posterior a
    `now + future_tolerance_minutes` tampoco se evalúa (S11 SEC-1).

    Si hay pronósticos de la ventana descartados por viejos o por adelantados, loguea
    `stale_forecasts count=<n> fields=<m> max_age_hours=<h>` (S11, D14) y
    `future_forecasts count=<n> fields=<m> tolerance_minutes=<t>` (S12a SEC-4), ambos desde
    una sola consulta agregada.

    No commitea: el llamador es dueño de la transacción. Devuelve la cantidad de
    notificaciones nuevas, contada por Postgres dentro de la misma sentencia (sin
    materializar el `RETURNING` en Python).
    """
    # Una sola réplica evalúa por vez. No se espera el lock (S14a): si otra evaluación o un
    # PATCH lo tiene tomado, el ciclo se saltea y el worker sigue al despacho.
    locked: bool = (
        await session.execute(select(func.pg_try_advisory_xact_lock(EVALUATOR_LOCK_KEY)))
    ).scalar_one()
    if not locked:
        logger.info("evaluation_skipped reason=lock_busy")
        return 0

    rearmed: int = (
        await session.execute(
            _build_rearm_statement(
                now=now,
                horizon_days=horizon_days,
                max_forecast_age_hours=max_forecast_age_hours,
                future_tolerance_minutes=future_tolerance_minutes,
                rearm_margin=rearm_margin,
            )
        )
    ).scalar_one()

    created: int = (
        await session.execute(
            _build_statement(
                now=now,
                horizon_days=horizon_days,
                max_forecast_age_hours=max_forecast_age_hours,
                future_tolerance_minutes=future_tolerance_minutes,
            )
        )
    ).scalar_one()
    logger.info(
        "alerts_evaluated created=%d rearmed=%d now=%s horizon_days=%d",
        created,
        rearmed,
        now,
        horizon_days,
    )

    stale_count, stale_fields, future_count, future_fields = (
        await session.execute(
            _build_out_of_window_statement(
                now=now,
                horizon_days=horizon_days,
                max_forecast_age_hours=max_forecast_age_hours,
                future_tolerance_minutes=future_tolerance_minutes,
            )
        )
    ).one()
    if stale_count:
        logger.warning(
            "stale_forecasts count=%d fields=%d max_age_hours=%d",
            stale_count,
            stale_fields,
            max_forecast_age_hours,
        )
    if future_count:
        logger.warning(
            "future_forecasts count=%d fields=%d tolerance_minutes=%d",
            future_count,
            future_fields,
            future_tolerance_minutes,
        )

    invalid_timezones: int = (
        await session.execute(_build_invalid_timezone_statement())
    ).scalar_one()
    if invalid_timezones:
        logger.warning("invalid_timezone_fields count=%d", invalid_timezones)
    return created
