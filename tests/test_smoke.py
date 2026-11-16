"""Smoke test de la infraestructura (debe pasar desde el día 0)."""

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_db_session_works(db_session: AsyncSession) -> None:
    assert (await db_session.execute(text("SELECT 1"))).scalar_one() == 1


async def test_app_boots(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
