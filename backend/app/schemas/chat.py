from pydantic import BaseModel, Field

from app.schemas.catalog import ProductCard
from app.schemas.order import OrderCard


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: int | None = None
    user_id: int | None = None


class SuggestedAction(BaseModel):
    label: str
    payload: dict = Field(default_factory=dict)


class ChatResponse(BaseModel):
    conversation_id: int
    answer: str
    intent: str
    products: list[ProductCard] = Field(default_factory=list)
    order: OrderCard | None = None
    suggested_actions: list[SuggestedAction] = Field(default_factory=list)
