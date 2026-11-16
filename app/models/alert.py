"""Modelo `Alert` (tabla `alerts`).

TODO(S1): implementar EXACTAMENTE según docs/DATA_MODEL.md §alerts
(columnas, tipos, nullability, CHECKs, UNIQUEs, índices, FKs con ondelete).
Usar SQLAlchemy 2.0 tipado: `Mapped[...]` + `mapped_column(...)`, heredar de
`Base` (y `TimestampMixin` si la tabla tiene created_at/updated_at).
"""
