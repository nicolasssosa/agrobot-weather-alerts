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

from app.models import Alert, Base, Field, Notification, User, WeatherForecast

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
    ],
    "fields": [
        ("id", "bigint", "NO", None, 64, 0),
        ("user_id", "bigint", "NO", None, 64, 0),
        ("name", "character varying", "NO", 120, None, None),
        ("latitude", "numeric", "NO", None, 9, 6),
        ("longitude", "numeric", "NO", None, 9, 6),
        ("created_at", "timestamp with time zone", "NO", None, None, None),
        ("updated_at", "timestamp with time zone", "NO", None, None, None),
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
    ],
    "alerts": [
        ("id", "bigint", "NO", None, 64, 0),
        ("field_id", "bigint", "NO", None, 64, 0),
        ("event_type", "character varying", "NO", 32, None, None),
        ("threshold", "smallint", "NO", None, 16, 0),
        ("is_active", "boolean", "NO", None, None, None),
        ("created_at", "timestamp with time zone", "NO", None, None, None),
        ("updated_at", "timestamp with time zone", "NO", None, None, None),
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
    ],
}

EXPECTED_UNIQUES: dict[str, set[frozenset[str]]] = {
    "users": {frozenset({"phone_number"})},
    "fields": {frozenset({"user_id", "name"})},
    "weather_forecasts": {frozenset({"field_id", "forecast_date", "event_type"})},
    "alerts": {frozenset({"field_id", "event_type"})},
    "notifications": {frozenset({"alert_id", "forecast_date"})},
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
        "notifications",
        "fk_notifications_alert_id_alerts",
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
    ("notifications", "ck_notifications_event_type"),
    ("notifications", "ck_notifications_notification_status"),
    ("notifications", "ck_notifications_probability_range"),
    ("notifications", "ck_notifications_threshold_range"),
}

EXPECTED_INDEXES = {
    "users": {"pk_users", "uq_users_phone_number"},
    "fields": {"pk_fields", "uq_fields_user_id_name"},
    "weather_forecasts": {
        "pk_weather_forecasts",
        "uq_weather_forecasts_field_id_forecast_date_event_type",
    },
    "alerts": {"pk_alerts", "uq_alerts_field_id_event_type", "ix_alerts_active_field_event"},
    "notifications": {
        "pk_notifications",
        "uq_notifications_alert_id_forecast_date",
        "ix_notifications_alert_id_created_at",
        "ix_notifications_pending_due",
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
        ("alerts", "id", "bigint", "BY DEFAULT"),
        ("fields", "id", "bigint", "BY DEFAULT"),
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
    assert actual == EXPECTED_UNIQUES


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

    timestamped_tables = ["users", "fields", "weather_forecasts", "alerts", "notifications"]
    for table in timestamped_tables:
        assert defaults[(table, "created_at")] == "now()"
        assert defaults[(table, "updated_at")] == "now()"

    assert defaults[("alerts", "is_active")] == "true"
    assert defaults[("notifications", "status")].startswith("'pending'")
    assert defaults[("notifications", "attempts")] == "0"
    assert defaults[("notifications", "next_attempt_at")] == "now()"
    assert defaults[("notifications", "last_error")] is None
    assert defaults[("notifications", "sent_at")] is None

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
    assert "WHERE is_active" in alerts_index

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


def test_initial_migration_is_revision_0001_without_down_revision() -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_current_head() == "0001"
    revision = script.get_revision("0001")
    assert revision is not None
    assert revision.down_revision is None
