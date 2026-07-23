from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class ToolDiagnostic(BaseModel):
    code: str
    severity: Literal["info", "warning", "error"] = "info"
    message: str
    recommended_action: str = "use_result"
    details: dict[str, Any] = Field(default_factory=dict)


class CatalogPreferenceDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brands: list[str] = Field(default_factory=list, max_length=8)
    excluded_brands: list[str] = Field(default_factory=list, max_length=8)
    excluded_usage: list[str] = Field(default_factory=list, max_length=8)
    max_price: Decimal | None = None
    connection_type: Literal["Wireless", "Wired"] | None = None
    usage: str | None = Field(default=None, max_length=64)


class CatalogSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    category: str | None = None
    brand: str | None = None
    brands: list[str] = Field(default_factory=list, max_length=8)
    excluded_brands: list[str] = Field(default_factory=list, max_length=8)
    excluded_usage: list[str] = Field(default_factory=list, max_length=8)
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    keywords: list[str] = Field(default_factory=list, max_length=12)
    usage: str | None = Field(default=None, max_length=64)
    sort: Literal["recommend", "sales", "price_asc", "price_desc", "stock"] = "recommend"
    preference_defaults: CatalogPreferenceDefaults = Field(
        default_factory=CatalogPreferenceDefaults
    )
    limit: int = Field(default=3, ge=1, le=20)


class CatalogSearchOutput(BaseModel):
    result_type: Literal["products", "empty"]
    products: list[ProductCard] = Field(default_factory=list)
    ranking_strategy: str
    query_plan: dict = Field(default_factory=dict)
    diagnostics: list[ToolDiagnostic] = Field(default_factory=list)


class CatalogCompareInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    comparison_level: Literal["sku", "spu"] = "sku"
    sku_ids: list[int] = Field(default_factory=list, max_length=10)
    spu_ids: list[int] = Field(default_factory=list, max_length=10)
    limit: int = Field(default=5, ge=2, le=10)

    @model_validator(mode="after")
    def validate_identifier_scope(self) -> "CatalogCompareInput":
        if self.comparison_level == "sku" and self.spu_ids:
            raise ValueError("spu_ids require comparison_level=spu")
        if self.comparison_level == "spu" and self.sku_ids:
            raise ValueError("sku_ids require comparison_level=sku")
        return self


class ProductComparisonItem(BaseModel):
    sku_id: int
    spu_id: int
    title: str
    brand: str
    category: str
    price: Decimal
    stock: int
    sku_sales_count: int = 0
    sku_sales_count_scope: Literal["sku"] = "sku"
    sales_count: int = 0
    sales_count_scope: Literal["spu"] = "spu"
    specs: dict[str, str] = Field(default_factory=dict)
    image_url: str | None = None


class CatalogSeriesSpecValue(BaseModel):
    value: str
    sku_count: int = Field(ge=1)
    in_stock_sku_count: int = Field(ge=0)


class CatalogSeriesSpecSummary(BaseModel):
    present_sku_count: int = Field(ge=1)
    missing_sku_count: int = Field(ge=0)
    values: list[CatalogSeriesSpecValue] = Field(default_factory=list)


class CatalogSeriesVariant(BaseModel):
    sku_id: int
    title: str
    price: Decimal
    stock: int
    sku_sales_count: int = 0
    specs: dict[str, str] = Field(default_factory=dict)
    image_url: str | None = None


class CatalogSeriesComparisonItem(BaseModel):
    spu_id: int
    title: str
    brand: str
    category: str
    sales_count: int = 0
    sku_count: int = Field(ge=1)
    in_stock_sku_count: int = Field(ge=0)
    total_stock: int = Field(ge=0)
    min_price: Decimal
    max_price: Decimal
    common_specs: dict[str, str] = Field(default_factory=dict)
    option_specs: dict[str, CatalogSeriesSpecSummary] = Field(default_factory=dict)
    variants: list[CatalogSeriesVariant] = Field(default_factory=list)


class CatalogSeriesFieldDifference(BaseModel):
    field: str
    shared_values: list[str] = Field(default_factory=list)
    left_only_values: list[str] = Field(default_factory=list)
    right_only_values: list[str] = Field(default_factory=list)
    left_missing_sku_count: int = Field(ge=0)
    right_missing_sku_count: int = Field(ge=0)


class CatalogSeriesPairDifference(BaseModel):
    left_spu_id: int
    right_spu_id: int
    fields: list[CatalogSeriesFieldDifference] = Field(default_factory=list)


class CatalogCompareOutput(BaseModel):
    result_type: Literal["comparison", "empty"]
    comparison_level: Literal["sku", "spu"] = "sku"
    products: list[ProductComparisonItem] = Field(default_factory=list)
    series: list[CatalogSeriesComparisonItem] = Field(default_factory=list)
    series_differences: list[CatalogSeriesPairDifference] = Field(default_factory=list)
    comparison_fields: list[str] = Field(default_factory=list)
    missing_fields: dict[int, list[str]] = Field(default_factory=dict)
    query_plan: dict = Field(default_factory=dict)
    diagnostics: list[ToolDiagnostic] = Field(default_factory=list)


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
    diagnostics: list[ToolDiagnostic] = Field(default_factory=list)


class OrderLookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    order_id: int | None = None
    query: str | None = Field(default=None, max_length=256)
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
