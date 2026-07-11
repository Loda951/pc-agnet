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
    decision: dict[str, Any]
    orchestrator_call_count: int
    tool_wave_count: int
    tool_waves: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    decision_header_streamed: bool
    response_streamed: bool
    boundary: dict[str, Any]
    parsed: dict[str, Any]
    history: list[dict[str, str]]
    evidence: list[EvidenceItem]
    products: list[ProductCard]
    order: OrderCard | None
    answer: str
    suggested_actions: list[dict[str, Any]]
