"""Tests del handler global de `RequestValidationError` (follow-up S2 SEC-4).

Un surrogate UTF-16 suelto o un float no finito en el `input` de un error de validación
hacía que el 422 de FastAPI no fuera JSON serializable (500). El handler los sanea, y además
acota profundidad y tamaño del `input` (que lo controla el cliente): sin cota, un body muy
anidado provocaba RecursionError (500) y uno gigante devolvía una respuesta de MBs.
"""

import json
from typing import Any

import pytest
from httpx import AsyncClient

from tests.conftest import MakeAlert, MakeField, MakeUser, auth

SURROGATE_JSON = '"a\\ud800b"'
ALERTS = "/api/v1/alerts"
DEEP_DEPTH = 3000


def _assert_fastapi_error_shape(detail: object, *, loc_prefix: str) -> list[dict[str, Any]]:
    """Valida que el 422 conserve el formato de FastAPI (`type`/`loc`/`msg` [+ `input`])."""
    assert isinstance(detail, list)
    assert detail
    for error in detail:
        assert isinstance(error, dict)
        assert {"type", "loc", "msg"} <= set(error)
        assert isinstance(error["type"], str)
        assert isinstance(error["msg"], str)
        assert isinstance(error["loc"], list)
        assert error["loc"][0] == loc_prefix
    return detail


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/api/v1/users",
            f'{{"phone_number": "+5491122334455", "name": {SURROGATE_JSON}}}',
        ),
        (
            "/api/v1/fields",
            f'{{"name": {SURROGATE_JSON}, "latitude": -34.6, "longitude": -58.4}}',
        ),
        (
            "/api/v1/alerts",
            f'{{"field_id": 1, "event_type": {SURROGATE_JSON}, "threshold": 70}}',
        ),
    ],
)
async def test_lone_surrogate_in_a_payload_returns_a_serializable_422(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, path: str, body: str
) -> None:
    user = await make_user()
    await make_field(user=user)

    response = await client.post(
        path, content=body, headers={**auth(user), "content-type": "application/json"}
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert detail
    # El `input` saneado es texto plano escapado, no un surrogate suelto.
    assert detail[0]["input"] == "a\\ud800b"
    # Si quedara un surrogate, `json.dumps` (ensure_ascii=True) lo re-escaparía, pero el
    # cuerpo ya se parseó bien: el round-trip completo es la prueba de serialización.
    assert "ud800" in json.dumps(detail)


@pytest.mark.parametrize("non_finite", ["NaN", "Infinity", "-Infinity"])
async def test_non_finite_threshold_returns_a_serializable_422(
    client: AsyncClient, make_user: MakeUser, non_finite: str
) -> None:
    user = await make_user()

    response = await client.post(
        "/api/v1/alerts",
        content=f'{{"field_id": 1, "event_type": "frost", "threshold": {non_finite}}}',
        headers={**auth(user), "content-type": "application/json"},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["input"] in {"nan", "inf", "-inf"}


async def test_validation_errors_keep_the_fastapi_shape(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()

    response = await client.post(
        "/api/v1/alerts",
        json={"field_id": 1, "event_type": "frost", "threshold": 0},
        headers=auth(user),
    )

    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert {"type", "loc", "msg", "input"} <= set(error)
    assert error["loc"] == ["body", "threshold"]
    assert error["input"] == 0
    assert isinstance(error["type"], str)
    assert isinstance(error["msg"], str)


@pytest.mark.parametrize(
    ("method", "path_template", "request_kwargs", "loc_prefix"),
    [
        (
            "post",
            "/api/v1/users",
            {"json": {"phone_number": "abc", "name": "Ana"}},
            "body",
        ),
        (
            "post",
            "/api/v1/fields",
            {"json": {"name": "", "latitude": 91, "longitude": 0}},
            "body",
        ),
        (
            "get",
            "/api/v1/fields/{field_id}/forecasts",
            {"params": {"date_from": "abc"}},
            "query",
        ),
        ("get", "/api/v1/alerts", {"params": {"limit": 0}}, "query"),
    ],
)
async def test_other_endpoints_keep_the_fastapi_422_shape(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    method: str,
    path_template: str,
    request_kwargs: dict[str, Any],
    loc_prefix: str,
) -> None:
    # SEC-4: el handler global no debe cambiar el contrato de los 422 ya existentes.
    user = await make_user()
    field = await make_field(user=user)

    response = await client.request(
        method, path_template.format(field_id=field.id), headers=auth(user), **request_kwargs
    )

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/json"
    _assert_fastapi_error_shape(response.json()["detail"], loc_prefix=loc_prefix)


async def test_custom_date_range_422_keeps_its_string_detail(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    # El 422 de `date_from > date_to` es un HTTPException del router (no un
    # RequestValidationError): el handler de SEC-4 no lo debe interceptar.
    user = await make_user()
    field = await make_field(user=user)

    response = await client.get(
        f"/api/v1/fields/{field.id}/forecasts?date_from=2026-11-20&date_to=2026-11-19",
        headers=auth(user),
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "date_from must be <= date_to"}


@pytest.mark.parametrize("path", [ALERTS, "/api/v1/users"])
async def test_malformed_json_body_returns_a_serializable_422(
    client: AsyncClient, make_user: MakeUser, path: str
) -> None:
    user = await make_user()

    response = await client.post(
        path, content="{", headers={**auth(user), "content-type": "application/json"}
    )

    assert response.status_code == 422
    detail = _assert_fastapi_error_shape(response.json()["detail"], loc_prefix="body")
    assert detail[0]["type"] == "json_invalid"
    assert "ctx" in detail[0]


@pytest.mark.parametrize("body", ["[]", '"text"', "null", "123", "true"])
async def test_non_object_json_body_returns_a_serializable_422(
    client: AsyncClient, make_user: MakeUser, body: str
) -> None:
    user = await make_user()

    response = await client.post(
        ALERTS, content=body, headers={**auth(user), "content-type": "application/json"}
    )

    assert response.status_code == 422
    _assert_fastapi_error_shape(response.json()["detail"], loc_prefix="body")


@pytest.mark.parametrize("body", ["[]", "null", '"text"'])
async def test_non_object_patch_body_returns_a_serializable_422(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
    body: str,
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    alert = await make_alert(field)

    response = await client.patch(
        f"{ALERTS}/{alert.id}",
        content=body,
        headers={**auth(user), "content-type": "application/json"},
    )

    assert response.status_code == 422
    _assert_fastapi_error_shape(response.json()["detail"], loc_prefix="body")


@pytest.mark.parametrize("headers", [{}, {"content-type": "text/plain"}])
async def test_a_body_without_a_json_content_type_returns_a_serializable_422(
    client: AsyncClient, make_user: MakeUser, headers: dict[str, str]
) -> None:
    # FastAPI entrega los bytes crudos al schema; el handler los convierte a string
    # (un `bytes` no es serializable y sin saneo el 422 sería un 500).
    user = await make_user()
    body = '{"field_id": 1, "event_type": "frost", "threshold": 70}'

    response = await client.post(ALERTS, content=body, headers={**auth(user), **headers})

    assert response.status_code == 422
    detail = _assert_fastapi_error_shape(response.json()["detail"], loc_prefix="body")
    assert isinstance(detail[0]["input"], str)


async def test_model_validator_ctx_is_serializable(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    # El `model_validator` de `AlertUpdate` mete un `ValueError` en `ctx`; sin saneo ese
    # objeto no sería serializable y el 422 terminaría en 500.
    user = await make_user()
    field = await make_field(user=user)
    alert = await make_alert(field)

    response = await client.patch(f"{ALERTS}/{alert.id}", json={}, headers=auth(user))

    assert response.status_code == 422
    detail = _assert_fastapi_error_shape(response.json()["detail"], loc_prefix="body")
    assert detail[0]["type"] == "value_error"
    assert isinstance(detail[0]["ctx"], dict)
    assert isinstance(detail[0]["ctx"]["error"], str)


def _deeply_nested_json(shape: str, depth: int = DEEP_DEPTH) -> str:
    """JSON crudo con `depth` niveles de anidamiento (sin construir el objeto en Python)."""
    if shape == "list":
        return "[" * depth + "1" + "]" * depth
    return '{"a":' * depth + "1" + "}" * depth


@pytest.mark.parametrize("shape", ["list", "dict"])
@pytest.mark.parametrize(
    ("path", "body_template"),
    [
        (
            "/api/v1/users",
            '{"phone_number": "+5491122334455", "name": __NESTED__}',
        ),
        (
            "/api/v1/fields",
            '{"name": __NESTED__, "latitude": -34.6, "longitude": -58.4}',
        ),
        (
            ALERTS,
            '{"field_id": 1, "event_type": "frost", "threshold": __NESTED__}',
        ),
    ],
)
async def test_a_deeply_nested_field_value_returns_a_bounded_422(
    client: AsyncClient, make_user: MakeUser, path: str, body_template: str, shape: str
) -> None:
    # CR-1/SEC-1: el `input` del error es el valor anidado del cliente. Sin cota de
    # profundidad, el saneo recursivo levantaba RecursionError y el 422 terminaba en 500.
    user = await make_user()
    body = body_template.replace("__NESTED__", _deeply_nested_json(shape))

    response = await client.post(
        path, content=body, headers={**auth(user), "content-type": "application/json"}
    )

    assert response.status_code == 422
    detail = _assert_fastapi_error_shape(response.json()["detail"], loc_prefix="body")
    assert "<truncated>" in json.dumps(detail)


@pytest.mark.parametrize("shape", ["list", "dict"])
@pytest.mark.parametrize("path", ["/api/v1/users", "/api/v1/fields", ALERTS])
async def test_a_deeply_nested_body_returns_a_bounded_422(
    client: AsyncClient, make_user: MakeUser, path: str, shape: str
) -> None:
    # Mismo caso, pero el anidamiento es el body entero (el `input` del error de "campo
    # requerido" es el body completo).
    user = await make_user()

    response = await client.post(
        path,
        content=_deeply_nested_json(shape),
        headers={**auth(user), "content-type": "application/json"},
    )

    assert response.status_code == 422
    detail = _assert_fastapi_error_shape(response.json()["detail"], loc_prefix="body")
    assert "<truncated>" in json.dumps(detail)


async def test_a_one_megabyte_input_returns_a_small_422(
    client: AsyncClient, make_user: MakeUser
) -> None:
    # SEC-2: el `input` de 1 MB se trunca a 200 caracteres; el body del 422 queda chico.
    user = await make_user()
    huge_threshold = "9" * (1024 * 1024)

    response = await client.post(
        ALERTS,
        content=(
            f'{{"field_id": 1, "event_type": "frost", "threshold": {json.dumps(huge_threshold)}}}'
        ),
        headers={**auth(user), "content-type": "application/json"},
    )

    assert response.status_code == 422
    detail = _assert_fastapi_error_shape(response.json()["detail"], loc_prefix="body")
    sanitized = detail[0]["input"]
    assert isinstance(sanitized, str)
    assert len(sanitized) == 201  # 200 + "…"
    assert sanitized.endswith("…")
    assert len(response.content) < 10_000


async def test_a_long_list_input_is_truncated_to_twenty_items(
    client: AsyncClient, make_user: MakeUser
) -> None:
    # SEC-2: listas/dicts del `input` se limitan a los primeros 20 elementos + marcador.
    user = await make_user()

    response = await client.post(
        "/api/v1/users",
        json={"phone_number": "+5491122334455", "name": [f"item-{i}" for i in range(50)]},
        headers=auth(user),
    )

    assert response.status_code == 422
    sanitized = response.json()["detail"][0]["input"]
    assert sanitized[:20] == [f"item-{i}" for i in range(20)]
    assert sanitized[20] == "<truncated>"
    assert len(sanitized) == 21
