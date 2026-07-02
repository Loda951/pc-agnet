from datetime import datetime

from pydantic import BaseModel, Field


class CreateAfterSalesRequest(BaseModel):
    order_id: int
    order_item_id: int
    ticket_type: str = Field(pattern="^(return|exchange|refund|repair)$")
    reason: str = Field(min_length=2, max_length=255)
    description: str | None = None
    evidence: list[dict] = Field(default_factory=list)


class AfterSalesTicketCard(BaseModel):
    id: int
    order_id: int
    order_item_id: int
    ticket_type: str
    reason: str
    status: str
    created_at: datetime


class AfterSalesTicketResponse(BaseModel):
    ticket: AfterSalesTicketCard
