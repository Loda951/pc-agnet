from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.catalog import ProductCard
from app.schemas.order import OrderCard

BoundaryClassificationValue = Literal[
    "in_scope_auto", "human_handoff_required", "out_of_scope"
]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: int | None = None
    user_id: int | None = None


class SuggestedAction(BaseModel):
    label: str
    payload: dict = Field(default_factory=dict)


class BoundaryClassification(BaseModel):
    classification: BoundaryClassificationValue
    reason: str
    display_message: str


class ChatResponse(BaseModel):
    conversation_id: int
    answer: str
    intent: str
    boundary: BoundaryClassification
    products: list[ProductCard] = Field(default_factory=list)
    order: OrderCard | None = None
    suggested_actions: list[SuggestedAction] = Field(default_factory=list)
