import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.orders import OrderRepository
from app.schemas.order import OrderCard
from app.tools.schemas import OrderLookupInput, OrderLookupOutput, OrderSummary


class OrderToolService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def lookup(self, request: OrderLookupInput) -> OrderLookupOutput:
        repository = OrderRepository(self.session)
        order_id = request.order_id or _extract_order_id(request.query)
        if order_id is not None:
            order = await repository.get_order(request.user_id, order_id)
            return OrderLookupOutput(
                result_type="single_order" if order else "not_found",
                order=order,
            )

        orders = await repository.list_recent_orders(request.user_id, request.limit)
        if not orders:
            return OrderLookupOutput(result_type="not_found")
        return OrderLookupOutput(
            result_type="order_candidates",
            candidates=[_summary(order) for order in orders],
        )


def _summary(order: OrderCard) -> OrderSummary:
    first_item = order.items[0] if order.items else None
    return OrderSummary(
        id=order.id,
        status=order.status,
        status_label=order.status_label,
        pay_amount=order.pay_amount,
        created_at=order.created_at.isoformat(),
        item_count=len(order.items),
        first_item_name=first_item.sku_name if first_item else None,
        logistic_no=order.logistics.logistic_no if order.logistics else None,
    )


def _extract_order_id(query: str | None) -> int | None:
    if not query:
        return None
    # Demo order IDs are long numeric identifiers; short numbers are often prices or counts.
    matches = re.findall(r"(?<!\d)(\d{8,20})(?!\d)", query)
    if len(matches) != 1:
        return None
    return int(matches[0])
