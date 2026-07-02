from typing import Any, TypedDict

from app.schemas.catalog import ProductCard
from app.schemas.order import OrderCard


class AgentState(TypedDict, total=False):
    user_id: int
    conversation_id: int | None
    run_id: int
    message: str
    intent: str
    boundary: dict[str, Any]
    parsed: dict[str, Any]
    memory: list[dict[str, Any]]
    products: list[ProductCard]
    order: OrderCard | None
    answer: str
    suggested_actions: list[dict[str, Any]]
