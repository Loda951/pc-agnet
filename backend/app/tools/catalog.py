import re
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.intent import build_product_search
from app.models import Brand, Category, Sku, Spu
from app.repositories.catalog import CatalogRepository
from app.schemas.catalog import ProductCard, ProductSearchRequest
from app.tools.schemas import (
    CatalogCompareInput,
    CatalogCompareOutput,
    CatalogSearchInput,
    CatalogSearchOutput,
    ProductComparisonItem,
)

CATALOG_ALLOWED_TABLES = {
    "brand",
    "category",
    "spu",
    "sku",
    "goods_attribute_relation",
    "attribute_key",
    "attribute_value",
}


class CatalogQueryPlanner(Protocol):
    async def plan_search(self, request: CatalogSearchInput) -> ProductSearchRequest:
        ...

    async def plan_compare(self, request: CatalogCompareInput) -> ProductSearchRequest:
        ...


class RuleBasedCatalogQueryPlanner:
    """Default offline planner. An LLM/NL2SQL planner can replace this interface later."""

    async def plan_search(self, request: CatalogSearchInput) -> ProductSearchRequest:
        parsed = build_product_search(request.query)
        filters = {**parsed.filters, **request.filters}
        return ProductSearchRequest(
            query=request.query,
            category=request.category or parsed.category,
            min_price=request.min_price if request.min_price is not None else parsed.min_price,
            max_price=request.max_price if request.max_price is not None else parsed.max_price,
            filters=filters,
            limit=request.limit,
        )

    async def plan_compare(self, request: CatalogCompareInput) -> ProductSearchRequest:
        parsed = build_product_search(request.query)
        return ProductSearchRequest(
            query=request.query,
            category=parsed.category,
            max_price=parsed.max_price,
            filters=parsed.filters,
            limit=request.limit,
        )


class CatalogToolService:
    def __init__(
        self,
        session: AsyncSession,
        planner: CatalogQueryPlanner | None = None,
    ):
        self.session = session
        self.planner = planner or RuleBasedCatalogQueryPlanner()

    async def search(self, request: CatalogSearchInput) -> CatalogSearchOutput:
        plan = await self.planner.plan_search(request)
        products = await CatalogRepository(self.session).search_products(plan)
        products = _filter_brand(products, request.brand)
        return CatalogSearchOutput(
            result_type="products" if products else "empty",
            products=products[: request.limit],
            ranking_strategy="match_score_stock_price",
            query_plan=_dump_product_search(plan),
        )

    async def compare(self, request: CatalogCompareInput) -> CatalogCompareOutput:
        products = (
            await self._products_by_sku_ids(request.sku_ids)
            if request.sku_ids
            else await self._products_from_compare_query(request)
        )
        products = products[: request.limit]
        fields = _comparison_fields(products)
        return CatalogCompareOutput(
            result_type="comparison" if products else "empty",
            products=[_to_comparison_item(product) for product in products],
            comparison_fields=fields,
            missing_fields=_missing_fields(products, fields),
            query_plan={
                "mode": "direct_sku_ids" if request.sku_ids else "natural_language",
                "sku_ids": request.sku_ids,
                "query": request.query,
            },
        )

    async def _products_from_compare_query(
        self, request: CatalogCompareInput
    ) -> list[ProductCard]:
        plan = await self.planner.plan_compare(request)
        products = await CatalogRepository(self.session).search_products(plan)
        wanted_terms = _product_terms(request.query)
        if not wanted_terms:
            return products
        ranked = sorted(
            products,
            key=lambda product: (
                -_compare_term_score(product, wanted_terms),
                product.price,
                product.title,
            ),
        )
        matched = [
            product for product in ranked if _compare_term_score(product, wanted_terms) > 0
        ]
        return matched or ranked

    async def _products_by_sku_ids(self, sku_ids: list[int]) -> list[ProductCard]:
        if not sku_ids:
            return []
        stmt = (
            select(Sku, Spu, Brand, Category)
            .where(Sku.id.in_(sku_ids))
            .join(Spu, Sku.spu_id == Spu.id)
            .join(Brand, Spu.brand_id == Brand.id)
            .join(Category, Spu.category_id == Category.id)
        )
        rows = (await self.session.execute(stmt)).all()
        attributes = await CatalogRepository(self.session)._load_attributes(
            [sku.id for sku, *_ in rows]
        )
        by_id = {
            sku.id: ProductCard(
                spu_id=spu.id,
                sku_id=sku.id,
                title=sku.title,
                brand=brand.name,
                category=category.name,
                price=sku.price,
                stock=sku.stock,
                specs={str(key): str(value) for key, value in (sku.specs_json or {}).items()}
                | attributes.get(sku.id, {}),
                image_url=sku.image_url,
            )
            for sku, spu, brand, category in rows
        }
        return [by_id[sku_id] for sku_id in sku_ids if sku_id in by_id]


def validate_catalog_sql(sql: str) -> None:
    """Guard for future LLM/NL2SQL planners before SQL reaches an executor."""
    normalized = re.sub(r"\s+", " ", sql.strip().lower())
    if not normalized.startswith("select "):
        raise ValueError("catalog SQL must be a SELECT statement")
    forbidden = {
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "truncate",
        "create",
        "grant",
        "revoke",
    }
    if any(re.search(rf"\b{word}\b", normalized) for word in forbidden):
        raise ValueError("catalog SQL contains a forbidden operation")
    if " limit " not in f" {normalized} ":
        raise ValueError("catalog SQL must include LIMIT")

    referenced_tables = set(re.findall(r"\b(?:from|join)\s+([a-z_][a-z0-9_]*)", normalized))
    unknown = referenced_tables - CATALOG_ALLOWED_TABLES
    if unknown:
        raise ValueError(f"catalog SQL references non-catalog tables: {', '.join(sorted(unknown))}")


def _dump_product_search(request: ProductSearchRequest) -> dict:
    return request.model_dump(mode="json")


def _filter_brand(products: list[ProductCard], brand: str | None) -> list[ProductCard]:
    if not brand:
        return products
    brand_lower = brand.lower()
    return [product for product in products if brand_lower in product.brand.lower()]


def _to_comparison_item(product: ProductCard) -> ProductComparisonItem:
    return ProductComparisonItem(**product.model_dump(mode="python"))


def _comparison_fields(products: list[ProductCard]) -> list[str]:
    preferred = [
        "connection_type",
        "wireless",
        "max_dpi",
        "switches",
        "backlit",
        "microphone",
        "enclosure_type",
        "color",
    ]
    available = {key for product in products for key in product.specs}
    return [field for field in preferred if field in available] or sorted(available)[:8]


def _missing_fields(products: list[ProductCard], fields: list[str]) -> dict[int, list[str]]:
    return {
        product.sku_id: [field for field in fields if field not in product.specs]
        for product in products
        if any(field not in product.specs for field in fields)
    }


def _product_terms(query: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9][a-z0-9+.-]*|[\u4e00-\u9fff]+", query.lower())
    ignored = {"compare", "vs", "which", "better"}
    return [token for token in tokens if len(token) >= 2 and token not in ignored]


def _compare_term_score(product: ProductCard, terms: list[str]) -> int:
    haystack = " ".join(
        [
            product.title,
            product.brand,
            product.category,
            " ".join(str(value) for value in product.specs.values()),
        ]
    ).lower()
    return sum(1 for term in terms if term in haystack)
