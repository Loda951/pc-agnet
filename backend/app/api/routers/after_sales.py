from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.intent import classify_boundary
from app.api.dependencies import get_current_user
from app.core.database import get_session
from app.models import AppUser
from app.repositories.support import AfterSalesRepository
from app.schemas.after_sales import AfterSalesTicketCard, CreateAfterSalesRequest

router = APIRouter(prefix="/after-sales", tags=["after-sales"])


@router.get("", response_model=list[AfterSalesTicketCard])
async def list_after_sales_tickets(
    current_user: AppUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AfterSalesTicketCard]:
    return await AfterSalesRepository(session).list_tickets(current_user.id)


@router.get("/{ticket_id}", response_model=AfterSalesTicketCard)
async def get_after_sales_ticket(
    ticket_id: int,
    current_user: AppUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AfterSalesTicketCard:
    ticket = await AfterSalesRepository(session).get_ticket(current_user.id, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


@router.post("")
async def create_after_sales_ticket(
    request: CreateAfterSalesRequest,
    current_user: AppUser = Depends(get_current_user),
) -> None:
    boundary = classify_boundary(f"创建售后工单：{request.ticket_type}")
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=boundary.model_dump(mode="json"),
    )
