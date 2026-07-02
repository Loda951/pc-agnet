from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_session
from app.repositories.orders import OrderRepository
from app.schemas.order import OrderCard

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("/latest", response_model=OrderCard)
async def latest_order(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    user_id: int | None = Query(default=None),
) -> OrderCard:
    order = await OrderRepository(session).latest_order(user_id or settings.default_user_id)
    if order is None:
        raise HTTPException(status_code=404, detail="No order found")
    return order


@router.get("/{order_id}", response_model=OrderCard)
async def get_order(
    order_id: int,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    user_id: int | None = Query(default=None),
) -> OrderCard:
    order = await OrderRepository(session).get_order(user_id or settings.default_user_id, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order
