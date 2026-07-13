from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.catalog import ProductCard
from app.schemas.order import OrderCard


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False
    recommended_action: str = "stop"


class ToolExecutionResult(BaseModel):
    tool_name: str
    ok: bool
    output: dict | None = None
    error: ToolError | None = None


class CatalogPreferenceDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brands: list[str] = Field(default_factory=list, max_length=8)
    max_price: Decimal | None = None
    connection_type: Literal["Wireless", "Wired"] | None = None
    usage: str | None = Field(default=None, max_length=64)


class CatalogSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    category: str | None = None
    brand: str | None = None
    brands: list[str] = Field(default_factory=list, max_length=8)
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    usage: str | None = Field(default=None, max_length=64)
    preference_defaults: CatalogPreferenceDefaults = Field(
        default_factory=CatalogPreferenceDefaults
    )
    limit: int = Field(default=3, ge=1, le=20)


class CatalogSearchOutput(BaseModel):
    result_type: Literal["products", "empty"]
    products: list[ProductCard] = Field(default_factory=list)
    ranking_strategy: str
    query_plan: dict = Field(default_factory=dict)


class CatalogCompareInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


class CatalogFacetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = ""
    facet: Literal["category", "brand", "spec_key", "spec_value"] = "brand"
    category: str | None = None
    brand: str | None = None
    spec_key: str | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    limit: int = Field(default=20, ge=1, le=50)


class CatalogFacetItem(BaseModel):
    value: str
    count: int


class CatalogFacetOutput(BaseModel):
    result_type: Literal["facets", "empty"]
    facet: Literal["category", "brand", "spec_key", "spec_value"]
    items: list[CatalogFacetItem] = Field(default_factory=list)
    category: str | None = None
    brand: str | None = None
    spec_key: str | None = None
    query_plan: dict = Field(default_factory=dict)


class OrderLookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    model_config = ConfigDict(extra="forbid")

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
