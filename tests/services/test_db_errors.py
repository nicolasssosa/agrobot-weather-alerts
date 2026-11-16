"""Tests del helper compartido de errores de integridad (follow-up S2 CR-dup).

`is_unique_violation` decide si un `IntegrityError` es un 409 de negocio o un error que debe
propagarse: solo la violación de unicidad (sqlstate 23505) lo es.
"""

from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError

from app.services import _db_errors, alerts, fields, users
from app.services._db_errors import is_unique_violation

STATEMENT = "INSERT INTO alerts (field_id, event_type) VALUES ($1, $2)"
PARAMS = {"field_id": 1, "event_type": "frost"}


def _integrity_error(orig: object) -> IntegrityError:
    return IntegrityError(STATEMENT, PARAMS, orig)


def test_sqlstate_attribute_of_the_driver_is_a_unique_violation() -> None:
    assert is_unique_violation(_integrity_error(SimpleNamespace(sqlstate="23505"))) is True


def test_pgcode_attribute_is_a_unique_violation() -> None:
    assert is_unique_violation(_integrity_error(SimpleNamespace(pgcode="23505"))) is True


def test_sqlstate_of_the_wrapped_cause_is_a_unique_violation() -> None:
    orig = SimpleNamespace(__cause__=SimpleNamespace(sqlstate="23505"))

    assert is_unique_violation(_integrity_error(orig)) is True


def test_other_sqlstates_are_not_unique_violations() -> None:
    # 23503 = FK, 23514 = CHECK: no son duplicados y deben propagarse (nunca 409).
    assert is_unique_violation(_integrity_error(SimpleNamespace(sqlstate="23503"))) is False
    assert is_unique_violation(_integrity_error(SimpleNamespace(sqlstate="23514"))) is False


def test_missing_sqlstate_is_not_a_unique_violation() -> None:
    assert is_unique_violation(_integrity_error(None)) is False
    assert is_unique_violation(_integrity_error(object())) is False


def test_the_helper_is_the_same_object_in_every_service() -> None:
    # CR-dup: una sola implementación, importada por users, fields y alerts.
    assert users.is_unique_violation is _db_errors.is_unique_violation
    assert fields.is_unique_violation is _db_errors.is_unique_violation
    assert alerts.is_unique_violation is _db_errors.is_unique_violation
