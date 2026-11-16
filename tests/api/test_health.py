"""Tests de `GET /health`. Ver docs/TESTING.md §test_health."""

from collections.abc import AsyncIterator

from httpx import ASGITransport, AsyncClient

from app.core.db import get_session
from app.main import create_app


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


class _FailingSession:
    async def execute(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("database is down")


async def test_health_returns_503_when_the_database_query_fails() -> None:
    app = create_app()

    async def _override_session() -> AsyncIterator[_FailingSession]:
        yield _FailingSession()

    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    app.dependency_overrides.clear()
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
