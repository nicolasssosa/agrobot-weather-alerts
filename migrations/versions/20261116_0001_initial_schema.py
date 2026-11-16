"""initial schema

Crea las 5 tablas de docs/DATA_MODEL.md en el orden users -> fields -> weather_forecasts ->
alerts -> notifications (y las borra en orden inverso).

Revision ID: 0001
Revises:
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("phone_number", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("phone_number", name="uq_users_phone_number"),
    )

    op.create_table(
        "fields",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("latitude", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column("longitude", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("latitude BETWEEN -90 AND 90", name="latitude_range"),
        sa.CheckConstraint("longitude BETWEEN -180 AND 180", name="longitude_range"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_fields_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_fields"),
        sa.UniqueConstraint("user_id", "name", name="uq_fields_user_id_name"),
    )

    op.create_table(
        "weather_forecasts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("field_id", sa.BigInteger(), nullable=False),
        sa.Column("forecast_date", sa.Date(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("probability", sa.SmallInteger(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('frost', 'rain', 'hail', 'strong_wind', 'heat_wave')",
            name="event_type",
        ),
        sa.CheckConstraint("probability BETWEEN 0 AND 100", name="probability_range"),
        sa.ForeignKeyConstraint(
            ["field_id"],
            ["fields.id"],
            name="fk_weather_forecasts_field_id_fields",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_weather_forecasts"),
        sa.UniqueConstraint(
            "field_id",
            "forecast_date",
            "event_type",
            name="uq_weather_forecasts_field_id_forecast_date_event_type",
        ),
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("field_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("threshold", sa.SmallInteger(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('frost', 'rain', 'hail', 'strong_wind', 'heat_wave')",
            name="event_type",
        ),
        sa.CheckConstraint("threshold BETWEEN 1 AND 100", name="threshold_range"),
        sa.ForeignKeyConstraint(
            ["field_id"],
            ["fields.id"],
            name="fk_alerts_field_id_fields",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_alerts"),
        sa.UniqueConstraint("field_id", "event_type", name="uq_alerts_field_id_event_type"),
    )
    op.create_index(
        "ix_alerts_active_field_event",
        "alerts",
        ["field_id", "event_type"],
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("alert_id", sa.BigInteger(), nullable=False),
        sa.Column("forecast_date", sa.Date(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("probability", sa.SmallInteger(), nullable=False),
        sa.Column("threshold", sa.SmallInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        sa.CheckConstraint(
            "event_type IN ('frost', 'rain', 'hail', 'strong_wind', 'heat_wave')",
            name="event_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed')",
            name="notification_status",
        ),
        sa.CheckConstraint("probability BETWEEN 0 AND 100", name="probability_range"),
        sa.CheckConstraint("threshold BETWEEN 1 AND 100", name="threshold_range"),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["alerts.id"],
            name="fk_notifications_alert_id_alerts",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        sa.UniqueConstraint(
            "alert_id", "forecast_date", name="uq_notifications_alert_id_forecast_date"
        ),
    )
    op.create_index(
        "ix_notifications_alert_id_created_at",
        "notifications",
        ["alert_id", "created_at"],
    )
    op.create_index(
        "ix_notifications_pending_due",
        "notifications",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_pending_due", table_name="notifications")
    op.drop_index("ix_notifications_alert_id_created_at", table_name="notifications")
    op.drop_table("notifications")

    op.drop_index("ix_alerts_active_field_event", table_name="alerts")
    op.drop_table("alerts")

    op.drop_table("weather_forecasts")
    op.drop_table("fields")
    op.drop_table("users")
