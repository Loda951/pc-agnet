from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AfterSalesEvent, AfterSalesTicket
from app.repositories.orders import OrderRepository
from app.schemas.after_sales import AfterSalesTicketCard, CreateAfterSalesRequest


class AfterSalesRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_ticket(
        self, user_id: int, request: CreateAfterSalesRequest
    ) -> AfterSalesTicketCard | None:
        orders = OrderRepository(self.session)
        if not await orders.item_belongs_to_user(user_id, request.order_id, request.order_item_id):
            return None

        ticket = AfterSalesTicket(
            user_id=user_id,
            order_id=request.order_id,
            order_item_id=request.order_item_id,
            ticket_type=request.ticket_type,
            reason=request.reason,
            description=request.description,
            evidence_json=request.evidence,
            status="submitted",
        )
        self.session.add(ticket)
        await self.session.flush()
        self.session.add(
            AfterSalesEvent(
                ticket_id=ticket.id,
                actor="customer",
                event_type="submitted",
                note=request.reason,
                metadata_json={"description": request.description},
            )
        )
        await self.session.commit()
        await self.session.refresh(ticket)
        return AfterSalesTicketCard(
            id=ticket.id,
            order_id=ticket.order_id,
            order_item_id=ticket.order_item_id,
            ticket_type=ticket.ticket_type,
            reason=ticket.reason,
            status=ticket.status,
            created_at=ticket.created_at,
        )
