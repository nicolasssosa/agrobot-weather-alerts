"""Catálogo de zonas horarias que reconoce el Postgres del proceso (S12a CR-1/SEC-1).

Una sola definición para todos los consumidores (`app.services.fields` valida la zona al
crear un campo; `app.jobs.evaluator` arma el "hoy" local). Se declara como tabla para
consultarla con binds tipados, nunca con SQL interpolado (S12a CR-5).
"""

from typing import Final

from sqlalchemy import Column, MetaData, String, Table

#: Vista de sistema con las zonas que **este** Postgres sabe interpretar.
PG_TIMEZONE_NAMES: Final = Table(
    "pg_timezone_names",
    MetaData(),
    Column("name", String(64)),
    schema="pg_catalog",
)
