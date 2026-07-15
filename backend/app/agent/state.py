from typing import Any, TypedDict

from app.schemas.catalog import ProductCard
from app.schemas.chat import EvidenceItem
from app.schemas.context import PreparedTurn
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
    boundary: dict[str, Any]
    parsed: dict[str, Any]
    history: list[dict[str, str]]
    memory: list[dict[str, Any]]
    working_memory: dict[str, Any]
    evidence: list[EvidenceItem]
    products: list[ProductCard]
    catalog_tool_succeeded: bool
    order: OrderCard | None
    answer: str
    suggested_actions: list[dict[str, Any]]
    prepared_turn: PreparedTurn
    applied_memory_ids: list[int]
    memory_changes: list[dict[str, Any]]
    assistant_metadata: dict[str, Any]
