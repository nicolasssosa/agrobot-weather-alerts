"""Veracidad del contrato OpenAPI: lo publicado vs. lo que la API hace.

`tests/test_openapi_contract.py` verifica que `docs/openapi.json` esté sincronizado con
`app.openapi()`, que los endpoints protegidos declaren el security scheme/401 y que los
ejemplos de los schemas validen. Este archivo cierra el círculo (S8, RNF-07):

- provoca cada código de error declarado (401/404/409/422/503) contra la API real y valida
  el body contra el schema publicado (`ErrorResponse`, `HTTPValidationError`, etc.);
- valida cada respuesta de éxito (200/201/204) contra el schema declarado;
- envía los ejemplos documentados de request tal cual ("Try it out") y verifica que no den 422;
- valida los ejemplos de response contra su modelo;
- comprueba que el test de contrato falla si `docs/openapi.json` se modifica y que
  `python -m app.scripts.export_openapi` es determinista (en una copia temporal).
"""

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from pydantic import BaseModel, ValidationError

import tests.test_openapi_contract as contract
from app.core.db import get_session
from app.main import create_app
from app.models import Alert, Field, User, WeatherForecast
from app.models.enums import EventType
from app.schemas.alert import AlertCreate, AlertPage, AlertRead, AlertUpdate
from app.schemas.common import (
    AUTH_ERROR_DETAIL,
    BIGINT_MAX,
    ErrorResponse,
    HealthStatus,
    UnavailableStatus,
)
from app.schemas.field import FieldCreate, FieldPage, FieldRead
from app.schemas.forecast import ForecastRead
from app.schemas.notification import NotificationPage, NotificationRead
from app.schemas.user import UserCreate, UserRead
from tests.conftest import MakeAlert, MakeField, MakeForecast, MakeUser, auth

ROOT = Path(__file__).resolve().parent.parent
_SPEC: dict[str, Any] = create_app().openapi()
_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
_API = "/api/v1"


# --- Modelos para validar los bodies contra los schemas publicados ------------


class _ValidationErrorItem(BaseModel):
    """Item de `HTTPValidationError.detail` que genera FastAPI (docs/API.md §Errores)."""

    loc: list[str | int]
    msg: str
    type: str


class _HTTPValidationError(BaseModel):
    """422 de validación automático de FastAPI."""

    detail: list[_ValidationErrorItem]


_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
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
    "HealthStatus": HealthStatus,
    "UnavailableStatus": UnavailableStatus,
    "HTTPValidationError": _HTTPValidationError,
    "FieldPage": FieldPage,
    "AlertPage": AlertPage,
    "NotificationPage": NotificationPage,
}


def _ref_name(schema: dict[str, Any]) -> str | None:
    ref = schema.get("$ref")
    return ref.rsplit("/", 1)[-1] if isinstance(ref, str) else None


def _validate_body(schema: dict[str, Any], body: Any) -> None:
    """Valida `body` contra el schema OpenAPI declarado, usando los modelos Pydantic."""
    variants = schema.get("anyOf") or schema.get("oneOf")
    if variants is not None:
        errors: list[Exception] = []
        for variant in variants:
            try:
                _validate_body(variant, body)
            except (ValidationError, AssertionError) as exc:
                errors.append(exc)
            else:
                return
        raise AssertionError(f"{body!r} no valida contra ninguna variante {variants}: {errors}")
    name = _ref_name(schema)
    if name is not None:
        _SCHEMA_MODELS[name].model_validate(body)
        return
    if schema.get("type") == "array":
        assert isinstance(body, list), body
        for item in body:
            _validate_body(schema["items"], item)
        return
    raise AssertionError(f"schema sin modelo asociado: {schema}")


# --- Utilidades sobre el spec -------------------------------------------------


def _operations() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (method, path, operation)
        for path, path_item in _SPEC["paths"].items()
        for method, operation in path_item.items()
        if method in _HTTP_METHODS
    ]


def _responses(method: str, path: str) -> dict[str, Any]:
    return _SPEC["paths"][path][method]["responses"]


def _declared_error_codes(operation: dict[str, Any]) -> set[int]:
    return {int(code) for code in operation["responses"] if code.isdigit() and int(code) >= 400}


def _json_content(method: str, path: str, code: int) -> dict[str, Any]:
    return _responses(method, path)[str(code)]["content"]["application/json"]


def _request_example(method: str, path: str) -> dict[str, Any]:
    """Ejemplo documentado del request (media-type o schema referenciado)."""
    content = _SPEC["paths"][path][method]["requestBody"]["content"]["application/json"]
    if "example" in content:
        return dict(content["example"])
    name = _ref_name(content["schema"])
    assert name is not None
    examples = _SPEC["components"]["schemas"][name]["examples"]
    assert examples, f"{name} no publica examples"
    return dict(examples[0])


# --- Contexto de prueba -------------------------------------------------------


@dataclass
class Ctx:
    """Recursos reales para provocar cada caso del contrato."""

    client: AsyncClient
    user: User
    other_user: User
    field: Field
    other_field: Field
    alert: Alert
    other_alert: Alert
    forecast: WeatherForecast


@pytest.fixture
async def ctx(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> Ctx:
    user = await make_user()
    other_user = await make_user()
    field = await make_field(user=user)
    other_field = await make_field(user=other_user)
    alert = await make_alert(field=field)
    other_alert = await make_alert(field=other_field)
    forecast = await make_forecast(
        field,
        forecast_date=date(2026, 11, 17),
        event_type=EventType.FROST,
        probability=72,
    )
    return Ctx(
        client=client,
        user=user,
        other_user=other_user,
        field=field,
        other_field=other_field,
        alert=alert,
        other_alert=other_alert,
        forecast=forecast,
    )


async def _request(
    ctx: Ctx,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    field: Field | None = None,
    alert: Alert | None = None,
    raw_path_params: dict[str, str] | None = None,
) -> Response:
    path_params: dict[str, Any] = {
        "field_id": (field or ctx.field).id,
        "alert_id": (alert or ctx.alert).id,
    }
    if raw_path_params is not None:
        path_params.update(raw_path_params)
    return await ctx.client.request(
        method,
        path.format(**path_params),
        headers=headers,
        json=json_body,
        params=params,
    )


def _valid_body(method: str, path: str, ctx: Ctx) -> dict[str, Any] | None:
    """Body válido para los endpoints con requestBody (usado en los casos de 401)."""
    key = (method, path)
    if key == ("post", f"{_API}/users"):
        return {"phone_number": "+5491100005001", "name": "Probe User"}
    if key == ("post", f"{_API}/fields"):
        return {"name": "Probe Field", "latitude": -34.6, "longitude": -58.4}
    if key == ("post", f"{_API}/alerts"):
        return {"field_id": ctx.field.id, "event_type": "hail", "threshold": 50}
    if key == ("patch", f"{_API}/alerts/{{alert_id}}"):
        return {"threshold": 60}
    return None


# --- Probes de los códigos de error declarados --------------------------------


async def _probe_404(ctx: Ctx, method: str, path: str) -> Response:
    headers = auth(ctx.user)
    if (method, path) == ("post", f"{_API}/alerts"):
        return await _request(
            ctx,
            method,
            path,
            headers=headers,
            json_body={"field_id": ctx.other_field.id, "event_type": "rain", "threshold": 50},
        )
    if path in {f"{_API}/fields/{{field_id}}", f"{_API}/fields/{{field_id}}/forecasts"}:
        return await _request(ctx, method, path, headers=headers, field=ctx.other_field)
    if path == f"{_API}/alerts/{{alert_id}}":
        return await _request(
            ctx,
            method,
            path,
            headers=headers,
            alert=ctx.other_alert,
            json_body=_valid_body(method, path, ctx),
        )
    raise AssertionError(f"sin probe 404 para {method} {path}")


async def _probe_409(ctx: Ctx, method: str, path: str) -> Response:
    headers = auth(ctx.user)
    if (method, path) == ("post", f"{_API}/users"):
        payload = {"phone_number": "+5491100005002", "name": "Duplicated"}
        first = await _request(ctx, method, path, json_body=payload)
        assert first.status_code == 201, first.text
        return await _request(ctx, method, path, json_body=payload)
    if (method, path) == ("post", f"{_API}/fields"):
        payload = {"name": "Duplicated Field", "latitude": -34.6, "longitude": -58.4}
        first = await _request(ctx, method, path, headers=headers, json_body=payload)
        assert first.status_code == 201, first.text
        return await _request(ctx, method, path, headers=headers, json_body=payload)
    if (method, path) == ("post", f"{_API}/alerts"):
        payload = {"field_id": ctx.field.id, "event_type": "strong_wind", "threshold": 40}
        first = await _request(ctx, method, path, headers=headers, json_body=payload)
        assert first.status_code == 201, first.text
        return await _request(ctx, method, path, headers=headers, json_body=payload)
    if (method, path) == ("patch", f"{_API}/alerts/{{alert_id}}"):
        payload = {
            "field_id": ctx.field.id,
            "event_type": ctx.alert.event_type.value,
            "threshold": 55,
        }
        first = await _request(ctx, "post", f"{_API}/alerts", headers=headers, json_body=payload)
        assert first.status_code == 201, first.text
        return await _request(ctx, method, path, headers=headers, json_body={"threshold": 55})
    raise AssertionError(f"sin probe 409 para {method} {path}")


async def _probe_422(ctx: Ctx, method: str, path: str) -> Response:
    headers = auth(ctx.user)
    key = (method, path)
    if key == ("post", f"{_API}/users"):
        return await _request(ctx, method, path, json_body={"phone_number": "123", "name": "X"})
    if key == ("post", f"{_API}/fields"):
        return await _request(
            ctx,
            method,
            path,
            headers=headers,
            json_body={"name": "Bad", "latitude": 91, "longitude": 0},
        )
    if key == ("get", f"{_API}/fields"):
        return await _request(ctx, method, path, headers=headers, params={"limit": 0})
    if key == ("get", f"{_API}/fields/{{field_id}}"):
        return await _request(
            ctx, method, path, headers=headers, raw_path_params={"field_id": "not-an-int"}
        )
    if key == ("get", f"{_API}/fields/{{field_id}}/forecasts"):
        # 422 de validación de query param (el 422 de date_from > date_to tiene su test).
        return await _request(ctx, method, path, headers=headers, params={"event_type": "bogus"})
    if key == ("post", f"{_API}/alerts"):
        return await _request(
            ctx,
            method,
            path,
            headers=headers,
            json_body={"field_id": ctx.field.id, "event_type": "rain", "threshold": 101},
        )
    if key == ("get", f"{_API}/alerts"):
        return await _request(ctx, method, path, headers=headers, params={"limit": 1000})
    if key == ("get", f"{_API}/alerts/{{alert_id}}"):
        return await _request(
            ctx, method, path, headers=headers, raw_path_params={"alert_id": "not-an-int"}
        )
    if key == ("patch", f"{_API}/alerts/{{alert_id}}"):
        return await _request(ctx, method, path, headers=headers, json_body={})
    if key == ("delete", f"{_API}/alerts/{{alert_id}}"):
        return await _request(
            ctx, method, path, headers=headers, raw_path_params={"alert_id": "not-an-int"}
        )
    if key == ("get", f"{_API}/notifications"):
        return await _request(ctx, method, path, headers=headers, params={"status": "bogus"})
    raise AssertionError(f"sin probe 422 para {method} {path}")


async def _probe_error(ctx: Ctx, method: str, path: str, status_code: int) -> Response:
    if status_code == 401:
        return await _request(ctx, method, path, json_body=_valid_body(method, path, ctx))
    if status_code == 404:
        return await _probe_404(ctx, method, path)
    if status_code == 409:
        return await _probe_409(ctx, method, path)
    if status_code == 422:
        return await _probe_422(ctx, method, path)
    raise AssertionError(f"sin probe para {status_code} ({method} {path})")


_ERROR_CASES = [
    (method, path, code)
    for method, path, operation in _operations()
    for code in sorted(_declared_error_codes(operation))
    # GET /health declara 503, que se provoca con una sesión que falla (otra app).
    if (method, path) != ("get", "/health")
]


@pytest.mark.parametrize(("method", "path", "status_code"), _ERROR_CASES)
async def test_declared_error_response_is_real_and_matches_schema(
    method: str, path: str, status_code: int, ctx: Ctx
) -> None:
    """Cada error declarado se provoca de verdad y su body valida contra el schema."""
    response = await _probe_error(ctx, method, path, status_code)

    assert response.status_code == status_code, response.text
    content = _json_content(method, path, status_code)
    _validate_body(content["schema"], response.json())
    if "example" in content:
        # El ejemplo publicado es el body real (mismo `detail`), no solo válido.
        assert response.json() == content["example"]


async def test_health_503_is_real_and_matches_the_declared_schema() -> None:
    app = create_app()

    class _FailingSession:
        async def execute(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("database is down")

    async def _override_session() -> Any:
        yield _FailingSession()

    app.dependency_overrides[get_session] = _override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    app.dependency_overrides.clear()

    assert response.status_code == 503
    content = _json_content("get", "/health", 503)
    _validate_body(content["schema"], response.json())
    assert response.json() == content["example"] == {"status": "unavailable"}


# --- Respuestas de éxito ------------------------------------------------------


async def _probe_success(ctx: Ctx, method: str, path: str) -> Response:
    headers = auth(ctx.user)
    key = (method, path)
    if key == ("get", "/health"):
        return await ctx.client.get("/health")
    if key == ("post", f"{_API}/users"):
        return await _request(
            ctx,
            method,
            path,
            json_body={"phone_number": "+5491100005003", "name": "Success User"},
        )
    if key == ("get", f"{_API}/users/me"):
        return await _request(ctx, method, path, headers=headers)
    if key == ("post", f"{_API}/fields"):
        return await _request(
            ctx,
            method,
            path,
            headers=headers,
            json_body={"name": "Success Field", "latitude": -34.6, "longitude": -58.4},
        )
    if key == ("get", f"{_API}/fields"):
        return await _request(ctx, method, path, headers=headers)
    if key == ("get", f"{_API}/fields/{{field_id}}"):
        return await _request(ctx, method, path, headers=headers)
    if key == ("get", f"{_API}/fields/{{field_id}}/forecasts"):
        return await _request(ctx, method, path, headers=headers)
    if key == ("post", f"{_API}/alerts"):
        return await _request(
            ctx,
            method,
            path,
            headers=headers,
            json_body={"field_id": ctx.field.id, "event_type": "hail", "threshold": 50},
        )
    if key == ("get", f"{_API}/alerts"):
        return await _request(ctx, method, path, headers=headers)
    if key == ("get", f"{_API}/alerts/{{alert_id}}"):
        return await _request(ctx, method, path, headers=headers)
    if key == ("patch", f"{_API}/alerts/{{alert_id}}"):
        return await _request(ctx, method, path, headers=headers, json_body={"threshold": 60})
    if key == ("delete", f"{_API}/alerts/{{alert_id}}"):
        return await _request(ctx, method, path, headers=headers)
    if key == ("get", f"{_API}/notifications"):
        return await _request(ctx, method, path, headers=headers)
    raise AssertionError(f"sin probe de éxito para {method} {path}")


_SUCCESS_CASES = [
    (method, path, int(code))
    for method, path, operation in _operations()
    for code in operation["responses"]
    if code.isdigit() and 200 <= int(code) < 300
]


@pytest.mark.parametrize(("method", "path", "status_code"), _SUCCESS_CASES)
async def test_declared_success_response_matches_schema(
    method: str, path: str, status_code: int, ctx: Ctx
) -> None:
    response = await _probe_success(ctx, method, path)

    assert response.status_code == status_code, response.text
    content = _responses(method, path)[str(status_code)].get("content")
    if status_code == 204:
        assert not content, content
        assert response.content == b""
        return
    assert content is not None, f"{method} {path} {status_code} sin content declarado"
    _validate_body(content["application/json"]["schema"], response.json())


# --- Matriz declarada vs. documentada (docs/API.md) ---------------------------

_EXPECTED_ERROR_CODES: dict[tuple[str, str], set[int]] = {
    ("get", "/health"): {503},
    ("post", f"{_API}/users"): {409, 422},
    ("get", f"{_API}/users/me"): {401},
    ("post", f"{_API}/fields"): {401, 409, 422},
    ("get", f"{_API}/fields"): {401, 422},
    ("get", f"{_API}/fields/{{field_id}}"): {401, 404, 422},
    ("get", f"{_API}/fields/{{field_id}}/forecasts"): {401, 404, 422},
    ("post", f"{_API}/alerts"): {401, 404, 409, 422},
    ("get", f"{_API}/alerts"): {401, 422},
    ("get", f"{_API}/alerts/{{alert_id}}"): {401, 404, 422},
    ("patch", f"{_API}/alerts/{{alert_id}}"): {401, 404, 409, 422},
    ("delete", f"{_API}/alerts/{{alert_id}}"): {401, 404, 422},
    ("get", f"{_API}/notifications"): {401, 422},
}


def test_declared_error_codes_match_the_documented_matrix() -> None:
    actual = {(method, path): _declared_error_codes(op) for method, path, op in _operations()}
    assert actual == _EXPECTED_ERROR_CODES


async def test_codes_absent_from_the_contract_are_not_returned(ctx: Ctx) -> None:
    """Casos que podrían devolver un código no declarado: se comprueba que no ocurre."""
    # GET /users/me no declara 422: acepta query params desconocidos y sigue devolviendo 200.
    me = await _request(
        ctx, "get", f"{_API}/users/me", headers=auth(ctx.user), params={"foo": "bar"}
    )
    assert me.status_code == 200
    assert "422" not in _responses("get", f"{_API}/users/me")

    # POST /fields no declara 404: con un usuario inexistente responde 401, nunca 404.
    unknown_user = await ctx.client.post(
        f"{_API}/fields",
        json={"name": "Ghost", "latitude": -34.6, "longitude": -58.4},
        headers={"X-User-Id": str(BIGINT_MAX)},
    )
    assert unknown_user.status_code == 401
    assert "404" not in _responses("post", f"{_API}/fields")

    # PATCH /alerts declara 409 (colisión de umbral) pero la clave (campo, evento) sigue
    # siendo no editable y responde 422 por `extra="forbid"`.
    patch_key = await _request(
        ctx,
        "patch",
        f"{_API}/alerts/{{alert_id}}",
        headers=auth(ctx.user),
        json_body={"field_id": ctx.other_field.id},
    )
    assert patch_key.status_code == 422
    assert "409" in _responses("patch", f"{_API}/alerts/{{alert_id}}")


# --- Try it out: ejemplos documentados ---------------------------------------

_REQUEST_BODY_OPERATIONS = {
    ("post", f"{_API}/users"),
    ("post", f"{_API}/fields"),
    ("post", f"{_API}/alerts"),
    ("patch", f"{_API}/alerts/{{alert_id}}"),
}


def test_every_request_body_operation_documents_an_example() -> None:
    with_body = {
        (method, path) for method, path, operation in _operations() if "requestBody" in operation
    }
    assert with_body == _REQUEST_BODY_OPERATIONS
    for method, path in with_body:
        assert _request_example(method, path)


async def test_try_it_out_examples_work_without_editing_the_body(client: AsyncClient) -> None:
    user_example = _request_example("post", f"{_API}/users")
    created_user = await client.post(f"{_API}/users", json=user_example)
    assert created_user.status_code == 201, created_user.text
    user_id = created_user.json()["id"]
    _validate_body(_json_content("post", f"{_API}/users", 201)["schema"], created_user.json())
    headers = {"X-User-Id": str(user_id)}

    field_example = _request_example("post", f"{_API}/fields")
    created_field = await client.post(f"{_API}/fields", json=field_example, headers=headers)
    assert created_field.status_code == 201, created_field.text
    field_id = created_field.json()["id"]
    _validate_body(_json_content("post", f"{_API}/fields", 201)["schema"], created_field.json())

    # Único ajuste permitido: el id real del campo.
    alert_example = _request_example("post", f"{_API}/alerts")
    alert_payload = dict(alert_example)
    alert_payload["field_id"] = field_id
    assert set(alert_payload) == set(alert_example)
    created_alert = await client.post(f"{_API}/alerts", json=alert_payload, headers=headers)
    assert created_alert.status_code == 201, created_alert.text
    alert_id = created_alert.json()["id"]
    _validate_body(_json_content("post", f"{_API}/alerts", 201)["schema"], created_alert.json())

    patch_example = _request_example("patch", f"{_API}/alerts/{{alert_id}}")
    patched = await client.patch(f"{_API}/alerts/{alert_id}", json=patch_example, headers=headers)
    assert patched.status_code == 200, patched.text
    assert patched.json()["threshold"] == patch_example["threshold"]
    assert patched.json()["is_active"] is patch_example["is_active"]
    _validate_body(
        _json_content("patch", f"{_API}/alerts/{{alert_id}}", 200)["schema"], patched.json()
    )


# --- Ejemplos de response -----------------------------------------------------


def _schema_examples(content: dict[str, Any], schema: dict[str, Any]) -> list[Any]:
    if "example" in content:
        return [content["example"]]
    if "examples" in content:
        return [example.get("value") for example in content["examples"].values()]
    name = _ref_name(schema)
    if name is not None:
        examples = _SPEC["components"]["schemas"][name].get("examples", [])
        return json.loads(json.dumps(examples))
    if schema.get("type") == "array":
        return [[example] for example in _schema_examples({}, schema["items"])]
    return []


def test_response_examples_exist_and_validate_against_their_schemas() -> None:
    for method, path, operation in _operations():
        for code, declared in operation["responses"].items():
            content = declared.get("content", {}).get("application/json")
            if content is None:
                continue
            if code == "422" and "example" not in content and "examples" not in content:
                # 422 es el schema automático de FastAPI; no publica examples.
                continue
            examples = _schema_examples(content, content["schema"])
            assert examples, f"{method} {path} {code} no publica examples"
            for example in examples:
                _validate_body(content["schema"], example)


# --- Descripción de la API y por tag ------------------------------------------


def test_api_description_and_every_tag_are_described() -> None:
    description = _SPEC["info"]["description"]
    for expected in (
        "X-User-Id",
        "Authorize",
        "POST /api/v1/users",
        "POST /api/v1/fields",
        "POST /api/v1/alerts",
        "GET /api/v1/fields/{field_id}/forecasts",
        "GET /api/v1/notifications",
    ):
        assert expected in description, expected

    tags = {tag["name"]: tag.get("description") for tag in _SPEC.get("tags", [])}
    used = {tag for _, _, operation in _operations() for tag in operation.get("tags", [])}
    assert used, "ninguna operación declara tags"
    assert used <= set(tags), used - set(tags)
    for tag in used:
        assert tags[tag], f"tag {tag} sin descripción"


def test_every_operation_has_a_summary() -> None:
    for method, path, operation in _operations():
        assert operation.get("summary"), f"{method} {path} sin summary"


# --- Seguridad (sin cambios de comportamiento) --------------------------------


@pytest.mark.parametrize(
    "raw",
    ["abc", "-1", "+1", "0", " 1", "1 ", "9" * 20, str(2**63)],
)
async def test_invalid_x_user_id_returns_the_exact_401(client: AsyncClient, raw: str) -> None:
    response = await client.get(f"{_API}/users/me", headers={"X-User-Id": raw})

    assert response.status_code == 401
    assert response.json() == {"detail": AUTH_ERROR_DETAIL}


async def test_nonexistent_user_returns_the_exact_401(client: AsyncClient) -> None:
    response = await client.get(f"{_API}/users/me", headers={"X-User-Id": str(BIGINT_MAX)})

    assert response.status_code == 401
    assert response.json() == {"detail": AUTH_ERROR_DETAIL}


async def test_public_endpoints_do_not_require_the_header(client: AsyncClient) -> None:
    health = await client.get("/health")
    assert health.status_code == 200

    created = await client.post(
        f"{_API}/users", json={"phone_number": "+5491100005004", "name": "Public User"}
    )
    assert created.status_code == 201


# --- El test de contrato detecta docs/openapi.json modificado ----------------


def test_contract_test_fails_when_docs_openapi_json_is_modified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = json.loads(contract.OPENAPI_PATH.read_text(encoding="utf-8"))
    spec["info"]["version"] = "9.9.9"
    modified = tmp_path / "openapi.json"
    modified.write_text(
        json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(contract, "OPENAPI_PATH", modified)

    with pytest.raises(AssertionError, match="make openapi"):
        contract.test_openapi_json_is_in_sync()


def test_contract_test_fails_when_docs_openapi_json_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(contract, "OPENAPI_PATH", tmp_path / "missing.json")

    with pytest.raises(AssertionError, match=r"Falta docs/openapi\.json"):
        contract.test_openapi_json_is_in_sync()


# --- Script de exportación determinista ---------------------------------------


def test_export_openapi_script_is_deterministic(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "app", tmp_path / "app", ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "docs").mkdir()
    outputs: list[str] = []
    for seed in ("0", "12345"):
        result = subprocess.run(
            [sys.executable, "-m", "app.scripts.export_openapi"],
            cwd=tmp_path,
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(tmp_path)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        outputs.append((tmp_path / "docs" / "openapi.json").read_text(encoding="utf-8"))

    assert outputs[0] == outputs[1]
    assert outputs[0] == (ROOT / "docs" / "openapi.json").read_text(encoding="utf-8")


# --- 422 de forecasts: validación automática y rango de fechas -----------------


async def test_forecasts_date_range_422_status_matches_api_md(ctx: Ctx) -> None:
    response = await _request(
        ctx,
        "get",
        f"{_API}/fields/{{field_id}}/forecasts",
        headers=auth(ctx.user),
        params={"date_from": "2026-11-20", "date_to": "2026-11-18"},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "date_from must be <= date_to"}


async def test_forecasts_date_range_422_body_matches_the_declared_schema(ctx: Ctx) -> None:
    response = await _request(
        ctx,
        "get",
        f"{_API}/fields/{{field_id}}/forecasts",
        headers=auth(ctx.user),
        params={"date_from": "2026-11-20", "date_to": "2026-11-18"},
    )

    content = _json_content("get", f"{_API}/fields/{{field_id}}/forecasts", 422)
    _validate_body(content["schema"], response.json())
    # El body real es el ejemplo publicado de la forma `ErrorResponse`.
    assert response.json() == content["examples"]["date_range"]["value"]


def test_forecasts_422_declares_both_error_shapes_with_an_example_each() -> None:
    content = _json_content("get", f"{_API}/fields/{{field_id}}/forecasts", 422)
    refs = {
        variant.get("$ref")
        for variant in content["schema"].get("anyOf", []) + content["schema"].get("oneOf", [])
    }
    assert refs == {
        "#/components/schemas/HTTPValidationError",
        "#/components/schemas/ErrorResponse",
    }

    examples = content["examples"]
    assert set(examples) == {"invalid_query_param", "date_range"}
    for example in examples.values():
        _validate_body(content["schema"], example["value"])
