"""Contrato OpenAPI versionado y completo. Ver docs/BACKLOG.md §S8.

- `docs/openapi.json` debe coincidir con `app.openapi()` (regenerar con `make openapi`).
- Cada operación protegida declara el security scheme `X-User-Id` y el 401.
- Los recursos por id declaran 404 y los POST con unicidad declaran 409.
- Todos los ejemplos publicados validan contra su modelo Pydantic.
"""

import json
from typing import Any

import pytest
from httpx import AsyncClient

from app.main import create_app
from app.schemas.alert import AlertCreate, AlertPage, AlertRead, AlertUpdate
from app.schemas.common import ErrorResponse
from app.schemas.field import FieldCreate, FieldPage, FieldRead
from app.schemas.forecast import ForecastRead
from app.schemas.notification import NotificationPage, NotificationRead
from app.schemas.user import UserCreate, UserRead
from app.scripts.export_openapi import OPENAPI_PATH, render_openapi
from app.services.alerts import ALERT_LIMIT_DETAIL

_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})

#: Únicas operaciones sin `X-User-Id` (docs/API.md §Convenciones).
PUBLIC_OPERATIONS = {("post", "/api/v1/users"), ("get", "/health")}

#: Detalle exacto que devuelve cada endpoint con 404 (docs/API.md).
EXPECTED_404 = {
    ("post", "/api/v1/alerts"): "Field not found",
    ("get", "/api/v1/fields/{field_id}"): "Field not found",
    ("get", "/api/v1/fields/{field_id}/forecasts"): "Field not found",
    ("get", "/api/v1/alerts/{alert_id}"): "Alert not found",
    ("patch", "/api/v1/alerts/{alert_id}"): "Alert not found",
    ("delete", "/api/v1/alerts/{alert_id}"): "Alert not found",
}

#: Detalle exacto que devuelve cada POST con una constraint de unicidad.
EXPECTED_409 = {
    ("post", "/api/v1/users"): "User with this phone number already exists",
    ("post", "/api/v1/fields"): "Field name already exists",
    ("post", "/api/v1/alerts"): (
        "An alert with this field, event type and threshold already exists"
    ),
    ("patch", "/api/v1/alerts/{alert_id}"): (
        "An alert with this field, event type and threshold already exists"
    ),
}

#: Nombre del schema en `components.schemas` -> modelo Pydantic que lo valida.
_SCHEMA_MODELS: dict[str, type[Any]] = {
    "UserCreate": UserCreate,
    "UserRead": UserRead,
    "FieldCreate": FieldCreate,
    "FieldRead": FieldRead,
    "ForecastRead": ForecastRead,
    "AlertCreate": AlertCreate,
    "AlertUpdate": AlertUpdate,
    "AlertRead": AlertRead,
    "NotificationRead": NotificationRead,
    "ErrorResponse": ErrorResponse,
    "FieldPage": FieldPage,
    "AlertPage": AlertPage,
    "NotificationPage": NotificationPage,
}


def _spec() -> dict[str, Any]:
    return create_app().openapi()


def _serialized_openapi() -> str:
    return json.dumps(_spec(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (method, path, operation)
        for path, path_item in spec["paths"].items()
        for method, operation in path_item.items()
        if method in _HTTP_METHODS
    ]


def test_openapi_json_is_in_sync() -> None:
    expected = _serialized_openapi()
    assert render_openapi() == expected
    assert OPENAPI_PATH.exists(), "Falta docs/openapi.json: corré `make openapi`"
    assert OPENAPI_PATH.read_text(encoding="utf-8") == expected, (
        "OpenAPI desincronizado: corré `make openapi`"
    )


def test_security_scheme_is_api_key_header() -> None:
    scheme = _spec()["components"]["securitySchemes"]["X-User-Id"]
    assert scheme["type"] == "apiKey"
    assert scheme["in"] == "header"
    assert scheme["name"] == "X-User-Id"


def test_protected_operations_declare_security_and_401() -> None:
    spec = _spec()
    for method, path, operation in _operations(spec):
        key = (method, path)
        if key in PUBLIC_OPERATIONS:
            assert "security" not in operation, key
            assert "401" not in operation["responses"], key
            continue
        assert operation.get("security") == [{"X-User-Id": []}], key
        assert "401" in operation["responses"], key
        content = operation["responses"]["401"]["content"]["application/json"]
        assert content["schema"]["$ref"] == "#/components/schemas/ErrorResponse", key
        assert content["example"] == {"detail": "Missing or invalid X-User-Id header"}, key


def test_resource_endpoints_declare_404() -> None:
    spec = _spec()
    for (method, path), detail in EXPECTED_404.items():
        responses = spec["paths"][path][method]["responses"]
        assert "404" in responses, (method, path)
        assert responses["404"]["content"]["application/json"]["example"] == {"detail": detail}


def test_uniqueness_posts_declare_409() -> None:
    spec = _spec()
    for (method, path), detail in EXPECTED_409.items():
        responses = spec["paths"][path][method]["responses"]
        assert "409" in responses, (method, path)
        assert responses["409"]["content"]["application/json"]["example"] == {"detail": detail}


def test_post_alerts_409_documents_the_per_field_limit() -> None:
    """SEC-1: el 409 de POST /alerts también cubre el tope de alertas vivas por campo."""
    responses = _spec()["paths"]["/api/v1/alerts"]["post"]["responses"]

    description = responses["409"]["description"]
    assert ALERT_LIMIT_DETAIL in description
    assert "MAX_ALERTS_PER_FIELD" in description
    # El ejemplo sigue siendo el de unicidad (el contrato existente no cambia).
    assert responses["409"]["content"]["application/json"]["example"] == {
        "detail": "An alert with this field, event type and threshold already exists"
    }


_IDEMPOTENT_PATHS = ("/api/v1/users", "/api/v1/fields", "/api/v1/alerts")


@pytest.mark.parametrize("path", _IDEMPOTENT_PATHS)
def test_idempotent_posts_declare_the_key_and_replay_headers(path: str) -> None:
    """S10: los tres POST documentan `Idempotency-Key` y `Idempotent-Replayed`."""
    operation = _spec()["paths"][path]["post"]
    key_headers = [
        parameter
        for parameter in operation.get("parameters", [])
        if parameter["in"] == "header" and parameter["name"] == "Idempotency-Key"
    ]
    assert len(key_headers) == 1
    key_header = key_headers[0]
    assert key_header["required"] is False
    assert "Idempotency-Key" in key_header["description"]
    assert key_header["schema"]["anyOf"] == [{"type": "string"}, {"type": "null"}]

    replay = operation["responses"]["201"]["headers"]["Idempotent-Replayed"]
    assert replay["schema"] == {"type": "string", "enum": ["true"]}
    assert "replay" in replay["description"]


def test_only_idempotent_posts_declare_the_key_header() -> None:
    spec = _spec()
    declared = {
        (method, path)
        for method, path, operation in _operations(spec)
        if any(
            parameter["in"] == "header" and parameter["name"] == "Idempotency-Key"
            for parameter in operation.get("parameters", [])
        )
    }
    assert declared == {("post", path) for path in _IDEMPOTENT_PATHS}


@pytest.mark.parametrize(("schema_name", "model"), _SCHEMA_MODELS.items())
def test_examples_validate_against_their_models(schema_name: str, model: type[Any]) -> None:
    examples = _spec()["components"]["schemas"][schema_name].get("examples")
    assert examples, f"{schema_name} no publica examples en OpenAPI"
    for example in examples:
        model.model_validate(example)


async def test_docs_and_openapi_json_are_served(client: AsyncClient) -> None:
    assert (await client.get("/docs")).status_code == 200
    assert (await client.get("/openapi.json")).status_code == 200
