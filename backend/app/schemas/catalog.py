from decimal import Decimal

from pydantic import BaseModel, Field


class ProductSearchRequest(BaseModel):
    query: str = ""
    category: str | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    excluded_brands: list[str] = Field(default_factory=list, max_length=8)
    excluded_usage: list[str] = Field(default_factory=list, max_length=8)
    limit: int = Field(default=8, ge=1, le=20)


class ProductCard(BaseModel):
    spu_id: int
    sku_id: int
    title: str
    brand: str
    category: str
    price: Decimal
    stock: int
    sales_count: int = 0
    specs: dict[str, str] = Field(default_factory=dict)
    image_url: str | None = None


class ProductSearchResponse(BaseModel):
    products: list[ProductCard]
