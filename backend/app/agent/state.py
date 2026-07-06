from typing import Any, TypedDict

from app.schemas.catalog import ProductCard
from app.schemas.chat import EvidenceItem
from app.schemas.order import OrderCard


class AgentState(TypedDict, total=False):
    user_id: int
    conversation_id: int | None
    user_message_id: int
    run_id: int
    message: str
    intent: str
    boundary: dict[str, Any]
    parsed: dict[str, Any]
    history: list[dict[str, str]]
    memory: list[dict[str, Any]]
    working_memory: dict[str, Any]
    evidence: list[EvidenceItem]
    products: list[ProductCard]
    order: OrderCard | None
    answer: str
    suggested_actions: list[dict[str, Any]]
