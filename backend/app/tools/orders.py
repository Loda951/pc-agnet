import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.orders import OrderRepository
from app.schemas.order import OrderCard, OrderQueryMode
from app.tools.schemas import OrderLookupInput, OrderLookupOutput, OrderSummary

DEFAULT_RECENT_ORDER_LIMIT = 5
MAX_ORDER_WINDOW = 20


@dataclass(frozen=True)
class OrderQueryPlan:
    mode: OrderQueryMode
    limit: int
    offset: int = 0


class OrderToolService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def lookup(self, request: OrderLookupInput) -> OrderLookupOutput:
        repository = OrderRepository(self.session)
        order_id = (
            request.order_id
            if request.order_id is not None
            else _extract_order_id(request.query)
        )
        if order_id is not None:
            order = await repository.get_order(request.user_id, order_id)
            return OrderLookupOutput(
                result_type="single_order" if order else "not_found",
                order=order,
                query_mode="explicit",
                total_match_count=1 if order else 0,
                returned_count=1 if order else 0,
            )

        plan = _plan_query(request)
        if plan.mode == "count":
            total_count = await repository.count_orders(request.user_id)
            return OrderLookupOutput(
                result_type="order_count",
                query_mode="count",
                total_match_count=total_count,
                returned_count=0,
            )

        if plan.mode == "latest":
            total_count = await repository.count_orders(request.user_id)
            order = await repository.latest_order(request.user_id) if total_count else None
            return OrderLookupOutput(
                result_type="single_order" if order else "not_found",
                order=order,
                query_mode="latest",
                total_match_count=total_count,
                returned_count=1 if order else 0,
                is_exhaustive=total_count <= 1,
                next_offset=1 if total_count > 1 else None,
            )

        if plan.mode == "analysis":
            page = await repository.list_recent_orders_page(
                request.user_id,
                limit=MAX_ORDER_WINDOW,
                offset=0,
            )
            if not page.orders:
                return OrderLookupOutput(
                    result_type="not_found",
                    query_mode="analysis",
                    total_match_count=0,
                    returned_count=0,
                )
            returned_count = len(page.orders)
            is_exhaustive = returned_count >= page.total_count
            return OrderLookupOutput(
                result_type="order_analysis",
                analysis_orders=page.orders,
                query_mode="analysis",
                total_match_count=page.total_count,
                returned_count=returned_count,
                is_exhaustive=is_exhaustive,
                next_offset=None if is_exhaustive else returned_count,
            )

        page = await repository.list_recent_orders_page(
            request.user_id,
            limit=plan.limit,
            offset=plan.offset,
        )
        returned_count = len(page.orders)
        next_offset = page.offset + returned_count
        is_exhaustive = next_offset >= page.total_count
        if not page.orders:
            if plan.mode == "page" and page.total_count > 0:
                return OrderLookupOutput(
                    result_type="order_candidates",
                    query_mode="page",
                    total_match_count=page.total_count,
                    returned_count=0,
                    is_exhaustive=True,
                    offset=page.offset,
                )
            return OrderLookupOutput(
                result_type="not_found",
                query_mode=plan.mode,
                total_match_count=page.total_count,
                returned_count=0,
                is_exhaustive=is_exhaustive,
                offset=page.offset,
            )
        return OrderLookupOutput(
            result_type="order_candidates",
            candidates=[_summary(order) for order in page.orders],
            query_mode=plan.mode,
            total_match_count=page.total_count,
            returned_count=returned_count,
            is_exhaustive=is_exhaustive,
            offset=page.offset,
            next_offset=None if is_exhaustive else next_offset,
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


def _plan_query(request: OrderLookupInput) -> OrderQueryPlan:
    query = re.sub(r"\s+", "", request.query or "")
    if request.offset > 0 or any(marker in query for marker in ("下一页", "继续列", "继续看")):
        return OrderQueryPlan("page", min(request.limit, MAX_ORDER_WINDOW), request.offset)

    asks_for_list = any(
        marker in query
        for marker in ("最近", "列出", "列一下", "列表", "摘要", "全部", "所有", "给我看")
    )
    asks_for_count = any(
        marker in query
        for marker in (
            "几个订单",
            "多少订单",
            "多少个订单",
            "多少笔订单",
            "订单总数",
            "一共有",
            "总共有",
        )
    )
    if asks_for_count and not asks_for_list:
        return OrderQueryPlan("count", 1)

    if any(marker in query for marker in ("全部订单", "所有订单", "订单全部")):
        return OrderQueryPlan("all", MAX_ORDER_WINDOW)

    if any(
        marker in query
        for marker in (
            "最近一笔",
            "最近一个订单",
            "最新一笔",
            "最后一笔",
            "最近的那笔",
            "最新订单",
        )
    ):
        return OrderQueryPlan("latest", 1)

    requested_limit = _extract_requested_limit(query)
    if requested_limit is not None:
        return OrderQueryPlan("recent", requested_limit)

    if asks_for_count and asks_for_list:
        return OrderQueryPlan(
            "recent",
            min(request.limit or DEFAULT_RECENT_ORDER_LIMIT, MAX_ORDER_WINDOW),
        )
    if _is_plain_recent_query(query):
        return OrderQueryPlan(
            "recent",
            min(request.limit or DEFAULT_RECENT_ORDER_LIMIT, MAX_ORDER_WINDOW),
        )
    return OrderQueryPlan("analysis", MAX_ORDER_WINDOW)


def _extract_requested_limit(query: str) -> int | None:
    digit_match = re.search(r"最近(\d{1,2})(?:个|笔|条)?订单", query)
    if digit_match:
        return max(1, min(int(digit_match.group(1)), MAX_ORDER_WINDOW))

    chinese_numbers = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
        "十一": 11,
        "十二": 12,
        "十三": 13,
        "十四": 14,
        "十五": 15,
        "十六": 16,
        "十七": 17,
        "十八": 18,
        "十九": 19,
        "二十": 20,
    }
    chinese_match = re.search(
        r"最近([一二两三四五六七八九十]{1,3})(?:个|笔|条)?订单",
        query,
    )
    return chinese_numbers.get(chinese_match.group(1)) if chinese_match else None


def _is_plain_recent_query(query: str) -> bool:
    compact = re.sub(r"[，。！？、,.!?]", "", query.casefold())
    if not compact:
        return True
    if compact in {"showmyrecentorders", "showmyorders", "myorders", "recentorders"}:
        return True
    return bool(
        re.fullmatch(
            r"(?:请)?(?:帮我)?(?:查|查一下|查询|看|看看|列出|列一下)"
            r"(?:我)?(?:的)?(?:最近|最近的)?订单(?:摘要|列表)?",
            compact,
        )
        or re.fullmatch(r"(?:我)?(?:的)?(?:最近|最近的)?订单(?:摘要|列表)?", compact)
    )
