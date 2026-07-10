from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.catalog import ProductCard
from app.schemas.order import OrderCard


class ToolError(BaseModel):
    code: str
    message: str


class ToolExecutionResult(BaseModel):
    tool_name: str
    ok: bool
    output: dict | None = None
    error: ToolError | None = None


class CatalogSearchInput(BaseModel):
    query: str = Field(min_length=1)
    category: str | None = None
    brand: str | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    limit: int = Field(default=3, ge=1, le=20)


class CatalogSearchOutput(BaseModel):
    result_type: Literal["products", "empty"]
    products: list[ProductCard] = Field(default_factory=list)
    ranking_strategy: str
    query_plan: dict = Field(default_factory=dict)


class CatalogCompareInput(BaseModel):
    query: str = Field(min_length=1)
    sku_ids: list[int] = Field(default_factory=list, max_length=10)
    limit: int = Field(default=5, ge=2, le=10)


class ProductComparisonItem(BaseModel):
    sku_id: int
    spu_id: int
    title: str
    brand: str
    category: str
    price: Decimal
    stock: int
    sales_count: int = 0
    specs: dict[str, str] = Field(default_factory=dict)
    image_url: str | None = None


class CatalogCompareOutput(BaseModel):
    result_type: Literal["comparison", "empty"]
    products: list[ProductComparisonItem] = Field(default_factory=list)
    comparison_fields: list[str] = Field(default_factory=list)
    missing_fields: dict[int, list[str]] = Field(default_factory=dict)
    query_plan: dict = Field(default_factory=dict)


class OrderLookupInput(BaseModel):
    user_id: int
    order_id: int | None = None
    limit: int = Field(default=5, ge=1, le=20)


class OrderSummary(BaseModel):
    id: int
    status: int
    status_label: str
    pay_amount: Decimal
    created_at: str
    item_count: int
    first_item_name: str | None = None
    logistic_no: str | None = None


class OrderLookupOutput(BaseModel):
    result_type: Literal["single_order", "order_candidates", "not_found"]
    order: OrderCard | None = None
    candidates: list[OrderSummary] = Field(default_factory=list)


class DocumentSearchInput(BaseModel):
    query: str = Field(min_length=1)
    document_type: str | None = None
    limit: int = Field(default=3, ge=1, le=10)
    retrieval_mode: Literal["bm25", "vector", "hybrid"] = "hybrid"


class DocumentSearchHit(BaseModel):
    source_type: Literal["knowledge_document"] = "knowledge_document"
    source_id: int
    title: str
    document_type: str
    snippet: str
    score: float
    metadata: dict = Field(default_factory=dict)


class DocumentSearchOutput(BaseModel):
    result_type: Literal["documents", "empty"]
    documents: list[DocumentSearchHit] = Field(default_factory=list)
    search_strategy: Literal["bm25", "vector", "hybrid"] = "hybrid"
