import httpx
import redis.asyncio as redis
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_session

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def health(
    session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)
):
    checks: dict[str, str] = {}

    try:
        await session.execute(text("select 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # pragma: no cover - used for runtime diagnostics
        checks["postgres"] = f"error: {exc}"

    try:
        client = redis.from_url(settings.redis_url)
        await client.ping()
        await client.aclose()
        checks["redis"] = "ok"
    except Exception as exc:  # pragma: no cover
        checks["redis"] = f"error: {exc}"

    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(
                f"http://{settings.chroma_host}:{settings.chroma_port}/api/v1/heartbeat"
            )
            checks["chroma"] = "ok" if response.is_success else f"http {response.status_code}"
    except Exception as exc:  # pragma: no cover
        checks["chroma"] = f"error: {exc}"

    return {
        "status": "ok" if all(value == "ok" for value in checks.values()) else "degraded",
        "checks": checks,
    }
