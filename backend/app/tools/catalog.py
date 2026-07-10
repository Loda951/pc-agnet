import json
import re
from decimal import Decimal
from typing import Literal, Protocol

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.intent import build_product_search
from app.models import Brand, Category, Sku, Spu
from app.repositories.catalog import CATEGORY_ALIASES, CatalogRepository
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
ALLOWED_CATEGORIES = {
    "mouse",
    "keyboard",
    "headset",
    "headphone",
    "monitor",
    "speaker",
    "webcam",
    *CATEGORY_ALIASES.values(),
}
ALLOWED_FILTERS = {
    "backlit",
    "channels",
    "color",
    "connection_type",
    "enclosure_type",
    "field_of_view",
    "frame_rate",
    "frequency_response",
    "hand_orientation",
    "max_dpi",
    "microphone",
    "panel_type",
    "power_w",
    "refresh_rate",
    "resolution",
    "response_time_ms",
    "size_inch",
    "style",
    "switches",
    "tenkeyless",
    "tracking_method",
    "type",
    "weight_g",
    "wireless",
}
CATEGORY_FILTERS = {
    "mouse": {
        "color",
        "connection_type",
        "hand_orientation",
        "max_dpi",
        "tracking_method",
        "weight_g",
        "wireless",
    },
    "keyboard": {
        "backlit",
        "color",
        "connection_type",
        "style",
        "switches",
        "tenkeyless",
        "wireless",
    },
    "headset": {
        "color",
        "connection_type",
        "enclosure_type",
        "frequency_response",
        "microphone",
        "type",
        "wireless",
    },
    "headphone": {
        "color",
        "connection_type",
        "enclosure_type",
        "frequency_response",
        "microphone",
        "type",
        "wireless",
    },
    "monitor": {
        "color",
        "panel_type",
        "refresh_rate",
        "resolution",
        "response_time_ms",
        "size_inch",
    },
    "speaker": {
        "channels",
        "color",
        "connection_type",
        "power_w",
        "type",
        "wireless",
    },
    "webcam": {
        "color",
        "connection_type",
        "field_of_view",
        "frame_rate",
        "microphone",
        "resolution",
    },
}
ALLOWED_SORTS = {"recommend", "sales", "price_asc", "price_desc", "stock"}
BASE_COMPARISON_FIELDS = {"price", "stock", "brand", "category", "sales_count"}
UNSUPPORTED_QUERY_PATTERNS = {
    "growth": "current catalog data has no time-series sales history",
    "month over month": "current catalog data has no time-series sales history",
    "revenue": "current catalog tool does not support revenue analytics",
    "profit": "current catalog tool does not support profit analytics",
    "用户": "catalog tool cannot query user purchase statistics",
    "购买过": "catalog tool cannot query user purchase statistics",
}


class ProductQueryPlan(BaseModel):
    query: str = Field(min_length=1)
    category: str | None = None
    brands: list[str] = Field(default_factory=list, max_length=8)
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    keywords: list[str] = Field(default_factory=list, max_length=12)
    sort: Literal["recommend", "sales", "price_asc", "price_desc", "stock"] = "recommend"
    limit: int = Field(default=3, ge=1, le=20)
    supported: bool = True
    unsupported_reason: str | None = None
    planner: str = "rule_based"
    fallback_reason: str | None = None

    @model_validator(mode="after")
    def validate_price_range(self) -> "ProductQueryPlan":
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("min_price cannot be greater than max_price")
        return self


class CatalogComparePlan(BaseModel):
    query: str = Field(min_length=1)
    category: str | None = None
    items: list[str] = Field(default_factory=list, max_length=8)
    brands: list[str] = Field(default_factory=list, max_length=8)
    comparison_fields: list[str] = Field(default_factory=list, max_length=16)
    scenario: str | None = None
    limit: int = Field(default=5, ge=2, le=10)
    supported: bool = True
    unsupported_reason: str | None = None
    planner: str = "rule_based"
    fallback_reason: str | None = None


class CatalogQueryPlanner(Protocol):
    async def plan_search(self, request: CatalogSearchInput) -> ProductQueryPlan:
        ...

    async def plan_compare(self, request: CatalogCompareInput) -> CatalogComparePlan:
        ...


class RuleBasedCatalogQueryPlanner:
    """Default offline planner. An LLM/NL2SQL planner can replace this interface later."""

    async def plan_search(self, request: CatalogSearchInput) -> ProductQueryPlan:
        parsed = build_product_search(request.query)
        filters = {**parsed.filters, **request.filters}
        unsupported_reason = _unsupported_reason(request.query)
        return ProductQueryPlan(
            query=request.query,
            category=request.category or parsed.category,
            brands=[request.brand] if request.brand else [],
            min_price=request.min_price if request.min_price is not None else parsed.min_price,
            max_price=request.max_price if request.max_price is not None else parsed.max_price,
            filters=filters,
            limit=request.limit,
            supported=unsupported_reason is None,
            unsupported_reason=unsupported_reason,
        )

    async def plan_compare(self, request: CatalogCompareInput) -> CatalogComparePlan:
        parsed = build_product_search(request.query)
        unsupported_reason = _unsupported_reason(request.query)
        return CatalogComparePlan(
            query=request.query,
            category=parsed.category,
            items=_product_terms(request.query),
            comparison_fields=_comparison_fields_from_query(request.query),
            limit=request.limit,
            supported=unsupported_reason is None,
            unsupported_reason=unsupported_reason,
        )


class LLMCatalogQueryPlanner:
    """LLM planner that returns a guarded ProductQueryPlan JSON, not raw SQL."""

    def __init__(self, chat_model):
        self.chat_model = chat_model

    async def plan_search(self, request: CatalogSearchInput) -> ProductQueryPlan:
        return await self._plan(
            task="search",
            query=request.query,
            limit=request.limit,
            overrides={
                "category": request.category,
                "brands": [request.brand] if request.brand else [],
                "min_price": request.min_price,
                "max_price": request.max_price,
                "filters": request.filters,
            },
        )

    async def plan_compare(self, request: CatalogCompareInput) -> CatalogComparePlan:
        response = await self.chat_model.ainvoke(
            [
                SystemMessage(content=_catalog_compare_planner_system_prompt()),
                HumanMessage(
                    content=json.dumps(
                        {
                            "task": "compare",
                            "query": request.query,
                            "limit": request.limit,
                        },
                        ensure_ascii=False,
                    )
                ),
            ]
        )
        plan_data = _extract_json_object(_message_content_to_text(response.content))
        plan_data["query"] = request.query
        plan_data["limit"] = request.limit
        plan = CatalogComparePlan.model_validate(plan_data)
        plan.planner = "llm"
        return plan

    async def _plan(
        self,
        task: str,
        query: str,
        limit: int,
        overrides: dict,
    ) -> ProductQueryPlan:
        response = await self.chat_model.ainvoke(
            [
                SystemMessage(content=_catalog_planner_system_prompt()),
                HumanMessage(
                    content=json.dumps(
                        {
                            "task": task,
                            "query": query,
                            "limit": limit,
                            "explicit_overrides": _json_safe(overrides),
                        },
                        ensure_ascii=False,
                    )
                ),
            ]
        )
        raw_text = _message_content_to_text(response.content)
        plan_data = _extract_json_object(raw_text)
        plan_data["query"] = query
        plan_data["limit"] = limit
        plan = ProductQueryPlan.model_validate(plan_data)
        plan.planner = "llm"
        _apply_explicit_overrides(plan, overrides)
        return plan


class CatalogToolService:
    def __init__(
        self,
        session: AsyncSession,
        planner: CatalogQueryPlanner | None = None,
    ):
        self.session = session
        self.planner = planner or RuleBasedCatalogQueryPlanner()

    async def search(self, request: CatalogSearchInput) -> CatalogSearchOutput:
        plan = await self._safe_plan_search(request)
        if not plan.supported:
            return CatalogSearchOutput(
                result_type="empty",
                products=[],
                ranking_strategy="unsupported_query",
                query_plan=plan.model_dump(mode="json"),
            )

        product_request = _plan_to_product_search(plan)
        products = await CatalogRepository(self.session).search_products(product_request)
        products = _filter_brands(products, plan.brands)
        return CatalogSearchOutput(
            result_type="products" if products else "empty",
            products=products[: request.limit],
            ranking_strategy="match_score_sales_stock_price",
            query_plan=plan.model_dump(mode="json"),
        )

    async def compare(self, request: CatalogCompareInput) -> CatalogCompareOutput:
        compare_plan = None
        if request.sku_ids:
            products = await self._products_by_sku_ids(request.sku_ids)
        else:
            products, compare_plan = await self._products_from_compare_query(request)
        products = products[: request.limit]
        fields = (
            compare_plan.comparison_fields
            if compare_plan and compare_plan.comparison_fields
            else _comparison_fields(products)
        )
        return CatalogCompareOutput(
            result_type="comparison" if products else "empty",
            products=[_to_comparison_item(product) for product in products],
            comparison_fields=fields,
            missing_fields=_missing_fields(products, fields),
            query_plan={
                "mode": "direct_sku_ids" if request.sku_ids else "natural_language",
                "sku_ids": request.sku_ids,
                "query": request.query,
                "compare_plan": compare_plan.model_dump(mode="json") if compare_plan else None,
            },
        )

    async def _products_from_compare_query(
        self, request: CatalogCompareInput
    ) -> tuple[list[ProductCard], CatalogComparePlan]:
        plan = await self._safe_plan_compare(request)
        if not plan.supported:
            return [], plan
        search_plan = _compare_plan_to_product_query_plan(plan)
        products = await CatalogRepository(self.session).search_products(
            _plan_to_product_search(search_plan)
        )
        products = _filter_brands(products, plan.brands)
        wanted_terms = plan.items or _product_terms(request.query)
        if not wanted_terms:
            return products, plan
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
        return matched or ranked, plan

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
                sales_count=spu.sales_count,
                specs={str(key): str(value) for key, value in (sku.specs_json or {}).items()}
                | attributes.get(sku.id, {}),
                image_url=sku.image_url,
            )
            for sku, spu, brand, category in rows
        }
        return [by_id[sku_id] for sku_id in sku_ids if sku_id in by_id]

    async def _safe_plan_search(self, request: CatalogSearchInput) -> ProductQueryPlan:
        try:
            return validate_product_query_plan(await self.planner.plan_search(request))
        except (ValidationError, ValueError, TypeError) as exc:
            fallback = await RuleBasedCatalogQueryPlanner().plan_search(request)
            fallback.planner = "rule_based_fallback"
            fallback.fallback_reason = str(exc)
            return validate_product_query_plan(fallback)

    async def _safe_plan_compare(self, request: CatalogCompareInput) -> CatalogComparePlan:
        try:
            return validate_catalog_compare_plan(await self.planner.plan_compare(request))
        except (ValidationError, ValueError, TypeError) as exc:
            fallback = await RuleBasedCatalogQueryPlanner().plan_compare(request)
            fallback.planner = "rule_based_fallback"
            fallback.fallback_reason = str(exc)
            return validate_catalog_compare_plan(fallback)


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


def validate_product_query_plan(plan: ProductQueryPlan | dict) -> ProductQueryPlan:
    if isinstance(plan, dict):
        plan = ProductQueryPlan.model_validate(plan)

    if plan.category and plan.category.lower() not in ALLOWED_CATEGORIES:
        raise ValueError(f"unsupported category: {plan.category}")

    unknown_filters = {key for key in plan.filters if key.lower() not in ALLOWED_FILTERS}
    if unknown_filters:
        raise ValueError(f"unsupported catalog filters: {', '.join(sorted(unknown_filters))}")

    normalized_category = _canonical_category(plan.category)
    if normalized_category and normalized_category in CATEGORY_FILTERS:
        disallowed_for_category = {
            key for key in plan.filters if key.lower() not in CATEGORY_FILTERS[normalized_category]
        }
        if disallowed_for_category:
            raise ValueError(
                "unsupported filters for "
                f"{normalized_category}: {', '.join(sorted(disallowed_for_category))}"
            )

    if plan.sort not in ALLOWED_SORTS:
        raise ValueError(f"unsupported catalog sort: {plan.sort}")

    if plan.limit < 1 or plan.limit > 20:
        raise ValueError("catalog query limit must be between 1 and 20")

    if plan.min_price is not None and plan.min_price < 0:
        raise ValueError("min_price cannot be negative")
    if plan.max_price is not None and plan.max_price < 0:
        raise ValueError("max_price cannot be negative")

    if reason := _unsupported_reason(plan.query):
        plan.supported = False
        plan.unsupported_reason = plan.unsupported_reason or reason

    plan.filters = {key.lower(): str(value) for key, value in plan.filters.items()}
    plan.brands = [brand.strip() for brand in plan.brands if brand.strip()]
    plan.keywords = [keyword.strip() for keyword in plan.keywords if keyword.strip()]
    return plan


def validate_catalog_compare_plan(plan: CatalogComparePlan | dict) -> CatalogComparePlan:
    if isinstance(plan, dict):
        plan = CatalogComparePlan.model_validate(plan)

    if plan.category and plan.category.lower() not in ALLOWED_CATEGORIES:
        raise ValueError(f"unsupported category: {plan.category}")

    allowed_fields = BASE_COMPARISON_FIELDS | ALLOWED_FILTERS
    unknown_fields = {
        field for field in plan.comparison_fields if field.lower() not in allowed_fields
    }
    if unknown_fields:
        raise ValueError(f"unsupported comparison fields: {', '.join(sorted(unknown_fields))}")

    if plan.limit < 2 or plan.limit > 10:
        raise ValueError("catalog compare limit must be between 2 and 10")

    if reason := _unsupported_reason(plan.query):
        plan.supported = False
        plan.unsupported_reason = plan.unsupported_reason or reason

    plan.items = [item.strip() for item in plan.items if item.strip()]
    plan.brands = [brand.strip() for brand in plan.brands if brand.strip()]
    plan.comparison_fields = _dedupe_keep_order(
        [field.lower() for field in plan.comparison_fields if field.strip()]
    )
    return plan


def _plan_to_product_search(plan: ProductQueryPlan) -> ProductSearchRequest:
    if plan.planner.startswith("rule_based"):
        query_parts = [plan.query, *plan.keywords]
    else:
        # Natural language is already represented by structured fields. Using the
        # full user utterance as a SQL prefilter over titles/brands can over-constrain
        # catalog search, especially for intent words such as FPS or "recommend".
        query_parts = [*plan.brands, *_product_keywords(plan)]
    return ProductSearchRequest(
        query=" ".join(part for part in query_parts if part),
        category=plan.category,
        min_price=plan.min_price,
        max_price=plan.max_price,
        filters=plan.filters,
        limit=plan.limit,
    )


def _compare_plan_to_product_query_plan(plan: CatalogComparePlan) -> ProductQueryPlan:
    return ProductQueryPlan(
        query=plan.query,
        category=plan.category,
        brands=plan.brands,
        keywords=plan.items + ([plan.scenario] if plan.scenario else []),
        limit=plan.limit,
        supported=plan.supported,
        unsupported_reason=plan.unsupported_reason,
        planner=plan.planner,
        fallback_reason=plan.fallback_reason,
    )


def _filter_brands(products: list[ProductCard], brands: list[str]) -> list[ProductCard]:
    if not brands:
        return products
    lowered = [brand.lower() for brand in brands]
    return [
        product
        for product in products
        if any(brand in product.brand.lower() for brand in lowered)
    ]


def _unsupported_reason(query: str) -> str | None:
    lowered = query.lower()
    for pattern, reason in UNSUPPORTED_QUERY_PATTERNS.items():
        if pattern in lowered:
            return reason
    return None


def _canonical_category(category: str | None) -> str | None:
    if not category:
        return None
    lowered = category.lower()
    for canonical, mapped in {
        "mouse": {"mouse", "mice"},
        "keyboard": {"keyboard", "keyboards"},
        "headset": {"headset", "headphone", "headphones"},
        "monitor": {"monitor", "monitors"},
        "speaker": {"speaker", "speakers"},
        "webcam": {"webcam", "webcams"},
    }.items():
        if lowered in mapped:
            return canonical
    for english, alias in CATEGORY_ALIASES.items():
        if lowered == str(alias).lower():
            return _canonical_category(english)
    return lowered


def _product_keywords(plan: ProductQueryPlan) -> list[str]:
    ignored = {
        "recommend",
        "recommendation",
        "wireless",
        "wired",
        "fps",
        "gaming",
        "office",
        "mouse",
        "keyboard",
        "headset",
        "headphone",
        "monitor",
        "speaker",
        "webcam",
    }
    return [
        keyword
        for keyword in plan.keywords
        if keyword.lower() not in ignored and _canonical_category(keyword) not in CATEGORY_FILTERS
    ]


def _catalog_planner_system_prompt() -> str:
    return """
You are a catalog query planner for a PC peripherals ecommerce support agent.
Return exactly one JSON object. Do not return markdown.

You must not write SQL. You only fill ProductQueryPlan fields.

Allowed JSON fields:
- category: one of mouse, keyboard, headset, monitor, speaker, webcam, or null
- brands: array of brand names, max 8
- min_price: number or null
- max_price: number or null
- filters: object with allowed keys only
- keywords: array of short product intent keywords, max 12
- sort: one of recommend, sales, price_asc, price_desc, stock
- supported: boolean
- unsupported_reason: string or null

Allowed filters:
backlit, channels, color, connection_type, enclosure_type, field_of_view,
frame_rate, frequency_response, hand_orientation, max_dpi, microphone,
panel_type, power_w, refresh_rate, resolution, response_time_ms, size_inch,
style, switches, tenkeyless, tracking_method, type, weight_g, wireless.

Set supported=false when the user asks for analytics not available in the
catalog tables, such as time-series growth, revenue, profit, or user purchase
statistics. Otherwise set supported=true.
""".strip()


def _catalog_compare_planner_system_prompt() -> str:
    return """
You are a catalog comparison planner for a PC peripherals ecommerce support agent.
Return exactly one JSON object. Do not return markdown. Do not write SQL.

Allowed JSON fields:
- category: one of mouse, keyboard, headset, monitor, speaker, webcam, or null
- items: array of product names, model names, or distinguishing terms to compare
- brands: array of brand names, max 8
- comparison_fields: array of facts to compare
- scenario: short usage scenario string or null
- supported: boolean
- unsupported_reason: string or null

Allowed comparison fields:
price, stock, brand, category, sales_count, backlit, channels, color,
connection_type, enclosure_type, field_of_view, frame_rate, frequency_response,
hand_orientation, max_dpi, microphone, panel_type, power_w, refresh_rate,
resolution, response_time_ms, size_inch, style, switches, tenkeyless,
tracking_method, type, weight_g, wireless.

For FPS mouse comparisons, prefer fields: price, stock, sales_count,
connection_type, max_dpi, weight_g, hand_orientation.

Set supported=false when the user asks for analytics not available in catalog
tables, such as time-series growth, revenue, profit, or user purchase statistics.
""".strip()


def _extract_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            raise ValueError("LLM planner did not return a JSON object") from None
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("LLM planner JSON must be an object")
    return parsed


def _message_content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content)


def _apply_explicit_overrides(plan: ProductQueryPlan, overrides: dict) -> None:
    if overrides.get("category"):
        plan.category = overrides["category"]
    if overrides.get("brands"):
        plan.brands = overrides["brands"]
    if overrides.get("min_price") is not None:
        plan.min_price = overrides["min_price"]
    if overrides.get("max_price") is not None:
        plan.max_price = overrides["max_price"]
    if overrides.get("filters"):
        plan.filters = {**plan.filters, **overrides["filters"]}


def _json_safe(data: dict) -> dict:
    return json.loads(json.dumps(data, default=str))


def _comparison_fields_from_query(query: str) -> list[str]:
    lowered = query.lower()
    fields: list[str] = []
    if any(term in lowered for term in {"dpi", "sensor"}):
        fields.append("max_dpi")
    if any(term in lowered for term in {"weight", "light", "轻"}):
        fields.append("weight_g")
    if any(term in lowered for term in {"wireless", "wired", "无线", "有线"}):
        fields.extend(["connection_type", "wireless"])
    if any(term in lowered for term in {"switch", "axis", "轴"}):
        fields.append("switches")
    if any(term in lowered for term in {"refresh", "hz", "刷新"}):
        fields.append("refresh_rate")
    if any(term in lowered for term in {"resolution", "2k", "4k", "分辨率"}):
        fields.append("resolution")
    if any(term in lowered for term in {"mic", "microphone", "麦克风"}):
        fields.append("microphone")
    if "fps" in lowered:
        fields.extend(["price", "stock", "sales_count", "connection_type", "max_dpi", "weight_g"])
    return _dedupe_keep_order(fields)


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    deduped: list[str] = []
    for item in items:
        if item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


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
    def is_missing(product: ProductCard, field: str) -> bool:
        return field not in BASE_COMPARISON_FIELDS and field not in product.specs

    return {
        product.sku_id: [field for field in fields if is_missing(product, field)]
        for product in products
        if any(is_missing(product, field) for field in fields)
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
