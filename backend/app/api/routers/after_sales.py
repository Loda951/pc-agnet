from fastapi import APIRouter, HTTPException, status

from app.agent.intent import classify_boundary
from app.schemas.after_sales import CreateAfterSalesRequest

router = APIRouter(prefix="/after-sales", tags=["after-sales"])


@router.post("")
async def create_after_sales_ticket(
    request: CreateAfterSalesRequest,
) -> None:
    boundary = classify_boundary(f"创建售后工单：{request.ticket_type}")
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=boundary.model_dump(mode="json"),
    )
