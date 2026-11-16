"""Clasificación de errores de integridad de Postgres, compartida por los services.

Los services distinguen un conflicto de negocio (violación de unicidad) de cualquier otro
`IntegrityError` (FK, CHECK, ...), que debe propagarse sin convertirse en 409.
"""

from sqlalchemy.exc import IntegrityError

_UNIQUE_VIOLATION_SQLSTATE = "23505"


def is_unique_violation(exc: IntegrityError) -> bool:
    """Solo las violaciones de unicidad (sqlstate 23505) son un conflicto de negocio."""
    orig = exc.orig
    if getattr(orig, "sqlstate", None) == _UNIQUE_VIOLATION_SQLSTATE:
        return True
    if getattr(orig, "pgcode", None) == _UNIQUE_VIOLATION_SQLSTATE:
        return True
    return getattr(getattr(orig, "__cause__", None), "sqlstate", None) == _UNIQUE_VIOLATION_SQLSTATE
