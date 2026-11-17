"""Verificación del DDL real de la migración `0001` contra docs/DATA_MODEL.md.

`test_migrations.py` valida con `alembic check` que modelos y base migrada no divergen (aunque
`alembic check` no compara CHECKs); acá se inspecciona el catálogo de Postgres para comprobar
tipos, nullability, constraints, FKs con `ondelete`, índices (incluidos los parciales) y la
configuración de relaciones ORM.
"""

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Alert,
    Base,
    Field,
    Notification,
    User,
    WeatherForecast,
)

ROOT = Path(__file__).resolve().parent.parent

ColumnSpec = tuple[str, str, str, int | None, int | None, int | None]

# (column, data_type, is_nullable, char_length, numeric_precision, numeric_scale)
EXPECTED_COLUMNS: dict[str, list[ColumnSpec]] = {
    "users": [
        ("id", "bigint", "NO", None, 64, 0),
        ("phone_number", "character varying", "NO", 20, None, None),
        ("name", "character varying", "NO", 120, None, None),
        ("created_at", "timestamp with time zone", "NO", None, None, None),
        ("updated_at", "timestamp with time zone", "NO", None, None, None),
        # `0009` agrega las columnas al final (ADD COLUMN): zona y horario de silencio (S13).
        ("timezone", "character varying", "NO", 64, None, None),
        ("quiet_hours_start", "time without time zone", "YES", None, None, None),
        ("quiet_hours_end", "time without time zone", "YES", None, None, None),
    ],
    "fields": [
        ("id", "bigint", "NO", None, 64, 0),
        ("user_id", "bigint", "NO", None, 64, 0),
        ("name", "character varying", "NO", 120, None, None),
        ("latitude", "numeric", "NO", None, 9, 6),
        ("longitude", "numeric", "NO", None, 9, 6),
        ("created_at", "timestamp with time zone", "NO", None, None, None),
        ("updated_at", "timestamp with time zone", "NO", None, None, None),
        # `0007` agrega la columna al final (ADD COLUMN) con el default de D9.
        ("timezone", "character varying", "NO", 64, None, None),
    ],
    "weather_forecasts": [
        ("id", "bigint", "NO", None, 64, 0),
        ("field_id", "bigint", "NO", None, 64, 0),
        ("forecast_date", "date", "NO", None, None, None),
        ("event_type", "character varying", "NO", 32, None, None),
        ("probability", "smallint", "NO", None, 16, 0),
        ("source", "character varying", "NO", 50, None, None),
        ("fetched_at", "timestamp with time zone", "NO", None, None, None),
        ("created_at", "timestamp with time zone", "NO", None, None, None),
        ("updated_at", "timestamp with time zone", "NO", None, None, None),
        # `0005` agrega la columna al final (ADD COLUMN): el orden físico es el de la migración.
        ("issued_at", "timestamp with time zone", "NO", None, None, None),
    ],
    "alerts": [
        ("id", "bigint", "NO", None, 64, 0),
        ("field_id", "bigint", "NO", None, 64, 0),
        ("event_type", "character varying", "NO", 32, None, None),
        ("threshold", "smallint", "NO", None, 16, 0),
        ("is_active", "boolean", "NO", None, None, None),
        ("created_at", "timestamp with time zone", "NO", None, None, None),
        ("updated_at", "timestamp with time zone", "NO", None, None, None),
        # `0003` agrega la columna al final (ADD COLUMN): el orden físico es el de la migración.
        ("deleted_at", "timestamp with time zone", "YES", None, None, None),
    ],
    "notifications": [
        ("id", "bigint", "NO", None, 64, 0),
        ("alert_id", "bigint", "NO", None, 64, 0),
        ("forecast_date", "date", "NO", None, None, None),
        ("event_type", "character varying", "NO", 32, None, None),
        ("probability", "smallint", "NO", None, 16, 0),
        ("threshold", "smallint", "NO", None, 16, 0),
        ("status", "character varying", "NO", 16, None, None),
        ("attempts", "smallint", "NO", None, 16, 0),
        ("last_error", "text", "YES", None, None, None),
        ("next_attempt_at", "timestamp with time zone", "NO", None, None, None),
        ("sent_at", "timestamp with time zone", "YES", None, None, None),
        ("created_at", "timestamp with time zone", "NO", None, None, None),
        ("updated_at", "timestamp with time zone", "NO", None, None, None),
        # `0008` agrega la columna al final (ADD COLUMN): el orden físico es el de la migración.
        ("episode", "integer", "NO", None, 32, 0),
    ],
    "alert_day_states": [
        ("id", "bigint", "NO", None, 64, 0),
        ("alert_id", "bigint", "NO", None, 64, 0),
        ("forecast_date", "date", "NO", None, None, None),
        ("episode", "integer", "NO", None, 32, 0),
        ("armed", "boolean", "NO", None, None, None),
        ("created_at", "timestamp with time zone", "NO", None, None, None),
        ("updated_at", "timestamp with time zone", "NO", None, None, None),
    ],
    "idempotency_keys": [
        ("id", "bigint", "NO", None, 64, 0),
        ("user_id", "bigint", "NO", None, 64, 0),
        ("scope", "character varying", "NO", 64, None, None),
        ("key", "character varying", "NO", 255, None, None),
        ("request_hash", "character", "NO", 64, None, None),
        ("status_code", "smallint", "NO", None, 16, 0),
        ("response_body", "text", "NO", None, None, None),
        ("created_at", "timestamp with time zone", "NO", None, None, None),
        ("expires_at", "timestamp with time zone", "NO", None, None, None),
    ],
}

EXPECTED_UNIQUES: dict[str, set[frozenset[str]]] = {
    "users": {frozenset({"phone_number"})},
    "fields": {frozenset({"user_id", "name"})},
    "weather_forecasts": {frozenset({"field_id", "forecast_date", "event_type"})},
    # La unicidad de alerts pasó a un índice único parcial en `0003` (no es un constraint).
    "alerts": set(),
    # `0008`: el episodio entra en la clave (un día puede avisarse varias veces).
    "notifications": {frozenset({"alert_id", "forecast_date", "episode"})},
    "alert_day_states": {frozenset({"alert_id", "forecast_date"})},
    # `0006`: solo `POST /alerts` guarda claves; `user_id` es NOT NULL y el UNIQUE ya no
    # necesita `NULLS NOT DISTINCT`.
    "idempotency_keys": {frozenset({"user_id", "scope", "key"})},
}

EXPECTED_FKS = {
    ("fields", "fk_fields_user_id_users", "users", "c", ("user_id",)),
    (
        "weather_forecasts",
        "fk_weather_forecasts_field_id_fields",
        "fields",
        "c",
        ("field_id",),
    ),
    ("alerts", "fk_alerts_field_id_fields", "fields", "c", ("field_id",)),
    (
        "idempotency_keys",
        "fk_idempotency_keys_user_id_users",
        "users",
        "c",
        ("user_id",),
    ),
    (
        "notifications",
        "fk_notifications_alert_id_alerts",
        "alerts",
        "c",
        ("alert_id",),
    ),
    (
        "alert_day_states",
        "fk_alert_day_states_alert_id_alerts",
        "alerts",
        "c",
        ("alert_id",),
    ),
}

# Nombres generados por la naming convention (`ck_<tabla>_<name>`), ver docs/DATA_MODEL.md.
EXPECTED_CHECKS = {
    ("fields", "ck_fields_latitude_range"),
    ("fields", "ck_fields_longitude_range"),
    ("weather_forecasts", "ck_weather_forecasts_event_type"),
    ("weather_forecasts", "ck_weather_forecasts_probability_range"),
    ("alerts", "ck_alerts_event_type"),
    ("alerts", "ck_alerts_threshold_range"),
    ("notifications", "ck_notifications_attempts_non_negative"),
    ("notifications", "ck_notifications_episode_positive"),
    ("notifications", "ck_notifications_event_type"),
    ("notifications", "ck_notifications_last_error_length"),
    ("notifications", "ck_notifications_notification_status"),
    ("notifications", "ck_notifications_probability_range"),
    ("notifications", "ck_notifications_threshold_range"),
    ("alert_day_states", "ck_alert_day_states_episode_positive"),
    ("users", "ck_users_phone_number_e164"),
    # `0009` (S13): el silencio es ambos extremos o ninguno, y la ventana no es vacía.
    ("users", "ck_users_quiet_hours_both_or_none"),
    ("users", "ck_users_quiet_hours_not_empty"),
}

EXPECTED_INDEXES = {
    "users": {"pk_users", "uq_users_phone_number"},
    "fields": {"pk_fields", "uq_fields_user_id_name"},
    "weather_forecasts": {
        "pk_weather_forecasts",
        "uq_weather_forecasts_field_id_forecast_date_event_type",
    },
    "alerts": {
        "pk_alerts",
        "uq_alerts_field_event_threshold_alive",
        "ix_alerts_active_field_event",
    },
    "notifications": {
        "pk_notifications",
        "uq_notifications_alert_id_forecast_date_episode",
        "ix_notifications_alert_id_created_at",
        "ix_notifications_pending_due",
    },
    "alert_day_states": {
        "pk_alert_day_states",
        "uq_alert_day_states_alert_id_forecast_date",
    },
    "idempotency_keys": {
        "pk_idempotency_keys",
        "uq_idempotency_keys_user_id_scope_key",
        "ix_idempotency_keys_expires_at",
    },
}

EXPECTED_RELATIONSHIPS: dict[type, dict[str, str]] = {
    User: {"fields": "Field"},
    Field: {"user": "User", "forecasts": "WeatherForecast", "alerts": "Alert"},
    WeatherForecast: {"field": "Field"},
    Alert: {"field": "Field", "notifications": "Notification"},
    Notification: {"alert": "Alert"},
}


@pytest.mark.parametrize("table", sorted(EXPECTED_COLUMNS))
async def test_table_columns_match_data_model(db_session: AsyncSession, table: str) -> None:
    rows = (
        await db_session.execute(
            text(
                """
                SELECT column_name, data_type, is_nullable,
                       character_maximum_length, numeric_precision, numeric_scale
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = :table
                ORDER BY ordinal_position
                """
            ),
            {"table": table},
        )
    ).all()
    assert [tuple(row) for row in rows] == EXPECTED_COLUMNS[table]


async def test_primary_keys_are_bigint_identity_columns(db_session: AsyncSession) -> None:
    rows = (
        await db_session.execute(
            text(
                """
                SELECT table_name, column_name, data_type, identity_generation
                FROM information_schema.columns
                WHERE table_schema = 'public' AND is_identity = 'YES'
                ORDER BY table_name
                """
            )
        )
    ).all()
    assert [tuple(row) for row in rows] == [
        ("alert_day_states", "id", "bigint", "BY DEFAULT"),
        ("alerts", "id", "bigint", "BY DEFAULT"),
        ("fields", "id", "bigint", "BY DEFAULT"),
        ("idempotency_keys", "id", "bigint", "BY DEFAULT"),
        ("notifications", "id", "bigint", "BY DEFAULT"),
        ("users", "id", "bigint", "BY DEFAULT"),
        ("weather_forecasts", "id", "bigint", "BY DEFAULT"),
    ]


async def test_unique_constraints_match_data_model(db_session: AsyncSession) -> None:
    rows = (
        await db_session.execute(
            text(
                """
                SELECT c.conrelid::regclass::text AS tbl,
                       array_agg(a.attname ORDER BY a.attname) AS cols
                FROM pg_constraint c
                CROSS JOIN LATERAL unnest(c.conkey) AS k(attnum)
                JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                WHERE c.contype = 'u' AND c.connamespace = 'public'::regnamespace
                GROUP BY c.oid, c.conrelid
                """
            )
        )
    ).all()
    actual: dict[str, set[frozenset[str]]] = {}
    for table, columns in rows:
        actual.setdefault(table, set()).add(frozenset(columns))
    # `alerts` ya no tiene UNIQUE constraints (su unicidad es un índice parcial en 0003).
    expected = {
        table: constraints for table, constraints in EXPECTED_UNIQUES.items() if constraints
    }
    assert actual == expected


async def test_idempotency_unique_does_not_use_nulls_not_distinct(
    db_session: AsyncSession,
) -> None:
    """`0006`: `user_id` es NOT NULL y el UNIQUE vuelve a la forma estándar."""
    nulls_not_distinct = (
        await db_session.execute(
            text(
                """
                SELECT i.indnullsnotdistinct
                FROM pg_index i
                JOIN pg_class c ON c.oid = i.indexrelid
                WHERE c.relname = 'uq_idempotency_keys_user_id_scope_key'
                """
            )
        )
    ).scalar_one()
    assert nulls_not_distinct is False


async def test_foreign_keys_are_bigint_and_on_delete_cascade(db_session: AsyncSession) -> None:
    rows = (
        await db_session.execute(
            text(
                """
                SELECT c.conrelid::regclass::text AS tbl,
                       c.conname,
                       c.confrelid::regclass::text AS ref_tbl,
                       c.confdeltype::text AS on_delete,
                       array_agg(a.attname ORDER BY a.attname) AS cols
                FROM pg_constraint c
                CROSS JOIN LATERAL unnest(c.conkey) AS k(attnum)
                JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace
                GROUP BY c.oid, c.conrelid, c.conname, c.confrelid, c.confdeltype
                """
            )
        )
    ).all()
    actual = {(tbl, name, ref, on_delete, tuple(cols)) for tbl, name, ref, on_delete, cols in rows}
    assert actual == EXPECTED_FKS


async def test_check_constraint_names_match_data_model_and_models(
    db_session: AsyncSession,
) -> None:
    rows = (
        await db_session.execute(
            text(
                """
                SELECT conrelid::regclass::text AS tbl, conname
                FROM pg_constraint
                WHERE contype = 'c' AND connamespace = 'public'::regnamespace
                """
            )
        )
    ).all()
    actual = {(table, name) for table, name in rows}
    assert actual == EXPECTED_CHECKS

    model_checks = {
        (table.name, constraint.name)
        for table in Base.metadata.sorted_tables
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert model_checks == EXPECTED_CHECKS


async def test_required_column_defaults_match_data_model(db_session: AsyncSession) -> None:
    rows = (
        await db_session.execute(
            text(
                """
                SELECT table_name, column_name, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public'
                """
            )
        )
    ).all()
    defaults = {(table, column): default for table, column, default in rows}

    timestamped_tables = [
        "users",
        "fields",
        "weather_forecasts",
        "alerts",
        "notifications",
        "alert_day_states",
    ]
    for table in timestamped_tables:
        assert defaults[(table, "created_at")] == "now()"
        assert defaults[(table, "updated_at")] == "now()"

    assert defaults[("alerts", "is_active")] == "true"
    assert defaults[("fields", "timezone")].startswith("'America/Argentina/Buenos_Aires'")
    assert defaults[("users", "timezone")].startswith("'America/Argentina/Buenos_Aires'")
    assert defaults[("users", "quiet_hours_start")] is None
    assert defaults[("users", "quiet_hours_end")] is None
    assert defaults[("notifications", "status")].startswith("'pending'")
    assert defaults[("notifications", "episode")] == "1"
    assert defaults[("notifications", "attempts")] == "0"
    assert defaults[("notifications", "next_attempt_at")] == "now()"
    assert defaults[("notifications", "last_error")] is None
    assert defaults[("notifications", "sent_at")] is None
    assert defaults[("idempotency_keys", "created_at")] == "now()"
    assert defaults[("idempotency_keys", "expires_at")] is None

    # Las PK usan IDENTITY, no un DEFAULT.
    for table in EXPECTED_COLUMNS:
        assert defaults[(table, "id")] is None


async def test_indexes_match_data_model(db_session: AsyncSession) -> None:
    rows = (
        await db_session.execute(
            text("SELECT tablename, indexname FROM pg_indexes WHERE schemaname = 'public'")
        )
    ).all()
    actual: dict[str, set[str]] = {}
    for table, index in rows:
        if table == "alembic_version":
            continue
        actual.setdefault(table, set()).add(index)
    assert actual == EXPECTED_INDEXES


async def test_partial_indexes_declare_expected_predicates_and_columns(
    db_session: AsyncSession,
) -> None:
    rows = (
        await db_session.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")
        )
    ).all()
    definitions = dict(rows)

    alerts_index = definitions["ix_alerts_active_field_event"]
    assert "(field_id, event_type)" in alerts_index
    assert "is_active" in alerts_index
    assert "deleted_at IS NULL" in alerts_index

    alive_unique = definitions["uq_alerts_field_event_threshold_alive"]
    assert "UNIQUE INDEX" in alive_unique
    assert "(field_id, event_type, threshold)" in alive_unique
    assert "deleted_at IS NULL" in alive_unique

    pending_index = definitions["ix_notifications_pending_due"]
    assert "(next_attempt_at)" in pending_index
    assert "'pending'" in pending_index
    assert "WHERE" in pending_index

    listing_index = definitions["ix_notifications_alert_id_created_at"]
    assert "(alert_id, created_at)" in listing_index
    assert "WHERE" not in listing_index


def test_relationships_are_lazy_raise_without_orm_delete_cascade() -> None:
    for model, expected in EXPECTED_RELATIONSHIPS.items():
        mapper = sa_inspect(model)
        assert set(mapper.relationships.keys()) == set(expected)
        for name, target in expected.items():
            relationship = mapper.relationships[name]
            assert relationship.mapper.class_.__name__ == target
            assert relationship.lazy == "raise"
            assert "delete" not in relationship.cascade
            assert "delete-orphan" not in relationship.cascade


async def test_migration_0002_check_constraints_are_in_the_catalog(
    db_session: AsyncSession,
) -> None:
    rows = (
        await db_session.execute(
            text(
                """
                SELECT conname, pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conname IN (
                    'ck_users_phone_number_e164', 'ck_notifications_last_error_length'
                )
                """
            )
        )
    ).all()
    definitions = dict(rows)

    assert "phone_number" in definitions["ck_users_phone_number_e164"]
    assert "[1-9]" in definitions["ck_users_phone_number_e164"]
    assert "last_error" in definitions["ck_notifications_last_error_length"]
    assert "1000" in definitions["ck_notifications_last_error_length"]


def test_migration_chain_is_0001_through_0009() -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_current_head() == "0009"
    initial = script.get_revision("0001")
    assert initial is not None
    assert initial.down_revision is None
    hardening = script.get_revision("0002")
    assert hardening is not None
    assert hardening.down_revision == "0001"
    soft_delete = script.get_revision("0003")
    assert soft_delete is not None
    assert soft_delete.down_revision == "0002"
    idempotency = script.get_revision("0004")
    assert idempotency is not None
    assert idempotency.down_revision == "0003"
    forecast_freshness = script.get_revision("0005")
    assert forecast_freshness is not None
    assert forecast_freshness.down_revision == "0004"
    idempotency_alerts_only = script.get_revision("0006")
    assert idempotency_alerts_only is not None
    assert idempotency_alerts_only.down_revision == "0005"
    field_timezone = script.get_revision("0007")
    assert field_timezone is not None
    assert field_timezone.down_revision == "0006"
    alert_episodes = script.get_revision("0008")
    assert alert_episodes is not None
    assert alert_episodes.down_revision == "0007"
    user_quiet_hours = script.get_revision("0009")
    assert user_quiet_hours is not None
    assert user_quiet_hours.down_revision == "0008"
