from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_session
from app.repositories.support import AfterSalesRepository
from app.schemas.after_sales import AfterSalesTicketResponse, CreateAfterSalesRequest

router = APIRouter(prefix="/after-sales", tags=["after-sales"])


@router.post("", response_model=AfterSalesTicketResponse)
async def create_after_sales_ticket(
    request: CreateAfterSalesRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    user_id: int | None = Query(default=None),
) -> AfterSalesTicketResponse:
    ticket = await AfterSalesRepository(session).create_ticket(
        user_id or settings.default_user_id,
        request,
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="Order item not found for current user")
    return AfterSalesTicketResponse(ticket=ticket)
