import asyncio
import json
import re
from decimal import Decimal
from typing import Literal, Protocol

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.intent import build_product_search
from app.models import Brand, Category, Sku, Spu
from app.repositories.catalog import CATEGORY_ALIASES, CatalogRepository
from app.schemas.catalog import ProductCard, ProductSearchRequest, ProductSpecCondition
from app.tools.schemas import (
    CatalogCompareInput,
    CatalogCompareOutput,
    CatalogFacetInput,
    CatalogFacetItem,
    CatalogFacetOutput,
    CatalogSearchInput,
    CatalogSearchOutput,
    ProductComparisonItem,
    ToolDiagnostic,
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
ALLOWED_USAGE_SCENARIOS = {
    "office",
    "gaming",
    "video_meeting",
    "live_streaming",
}
USAGE_SCENARIO_ALIASES = {
    "office": "office",
    "办公": "office",
    "工作": "office",
    "学习": "office",
    "gaming": "gaming",
    "game": "gaming",
    "fps": "gaming",
    "esports": "gaming",
    "游戏": "gaming",
    "电竞": "gaming",
    "video_meeting": "video_meeting",
    "video meeting": "video_meeting",
    "video conference": "video_meeting",
    "video conferencing": "video_meeting",
    "视频会议": "video_meeting",
    "远程会议": "video_meeting",
    "网课": "video_meeting",
    "开会": "video_meeting",
    "live_streaming": "live_streaming",
    "live streaming": "live_streaming",
    "livestream": "live_streaming",
    "直播": "live_streaming",
    "主播": "live_streaming",
    "开播": "live_streaming",
}
USAGE_MAPPING_VERSION = "v1"
USAGE_SCENARIO_CATEGORIES = {
    "office": ("keyboard", "monitor", "headset", "webcam"),
    "gaming": ("mouse", "keyboard", "headset", "monitor", "speaker"),
    "video_meeting": ("webcam", "headset"),
    "live_streaming": ("webcam",),
}
USAGE_CATEGORY_CONCURRENCY = 3


def _condition(
    key: str,
    operator: Literal["exact", "eq", "in", "gte", "lte"],
    *values: str,
) -> ProductSpecCondition:
    return ProductSpecCondition(key=key, operator=operator, values=list(values))


USAGE_SCENARIO_RULES: dict[
    tuple[str, str], dict[str, tuple[ProductSpecCondition, ...]]
] = {
    ("office", "keyboard"): {
        "preferred": (_condition("switches", "exact", "静音红轴"),),
    },
    ("office", "monitor"): {
        "preferred": (
            _condition("panel_type", "eq", "IPS"),
            _condition("resolution", "eq", "2560x1440"),
            _condition("size_inch", "eq", "27"),
        ),
    },
    ("office", "headset"): {
        "preferred": (
            _condition("microphone", "eq", "是"),
            _condition("enclosure_type", "eq", "封闭式"),
        ),
    },
    ("office", "webcam"): {
        "required": (_condition("microphone", "eq", "是"),),
        "preferred": (_condition("frame_rate", "gte", "60"),),
    },
    ("gaming", "mouse"): {
        "preferred": (
            _condition("weight_g", "lte", "65"),
            _condition("max_dpi", "gte", "16000"),
        ),
    },
    ("gaming", "keyboard"): {
        "preferred": (
            _condition("switches", "in", "磁轴", "线性红轴"),
        ),
    },
    ("gaming", "headset"): {
        "preferred": (
            _condition("microphone", "eq", "是"),
            _condition("enclosure_type", "eq", "封闭式"),
        ),
    },
    ("gaming", "monitor"): {
        "preferred": (
            _condition("refresh_rate", "gte", "144"),
            _condition("response_time_ms", "lte", "1"),
        ),
    },
    ("gaming", "speaker"): {
        "preferred": (_condition("channels", "in", "2.1", "5.1"),),
    },
    ("video_meeting", "webcam"): {
        "required": (_condition("microphone", "eq", "是"),),
        "preferred": (_condition("frame_rate", "gte", "60"),),
    },
    ("video_meeting", "headset"): {
        "required": (_condition("microphone", "eq", "是"),),
        "preferred": (_condition("enclosure_type", "eq", "封闭式"),),
    },
    ("live_streaming", "webcam"): {
        "required": (
            _condition("resolution", "in", "1080p HDR", "1440p", "4K"),
            _condition("frame_rate", "in", "60fps", "90fps"),
        ),
        "preferred": (_condition("field_of_view", "in", "90°", "103°"),),
    },
}
ALLOWED_SORTS = {"recommend", "sales", "price_asc", "price_desc", "stock"}
BASE_COMPARISON_FIELDS = {
    "price",
    "stock",
    "brand",
    "category",
    "sku_sales_count",
    "sales_count",
}
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
    excluded_brands: list[str] = Field(default_factory=list, max_length=8)
    excluded_usage: list[str] = Field(default_factory=list, max_length=8)
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    keywords: list[str] = Field(default_factory=list, max_length=12)
    usage_scenario: str | None = None
    usage_mapping: dict = Field(default_factory=dict)
    sort: Literal["recommend", "sales", "price_asc", "price_desc", "stock"] = "recommend"
    limit: int = Field(default=3, ge=1, le=20)
    supported: bool = True
    unsupported_reason: str | None = None
    planner: str = "rule_based"
    fallback_reason: str | None = None
    normalization_debug: dict = Field(default_factory=dict)

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
    normalization_debug: dict = Field(default_factory=dict)


class CatalogQueryPlanner(Protocol):
    async def plan_search(self, request: CatalogSearchInput) -> ProductQueryPlan: ...

    async def plan_compare(self, request: CatalogCompareInput) -> CatalogComparePlan: ...


class FacetQueryPlan(BaseModel):
    query: str = ""
    facet: Literal["category", "brand", "spec_key", "spec_value"] = "brand"
    category: str | None = None
    brand: str | None = None
    spec_key: str | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    limit: int = Field(default=20, ge=1, le=50)
    supported: bool = True
    unsupported_reason: str | None = None
    planner: str = "rule_based"
    normalization_debug: dict = Field(default_factory=dict)


class RuleBasedCatalogQueryPlanner:
    """Default offline planner. An LLM/NL2SQL planner can replace this interface later."""

    async def plan_search(self, request: CatalogSearchInput) -> ProductQueryPlan:
        parsed = build_product_search(request.query)
        defaults = request.preference_defaults
        default_filters = (
            {"connection_type": defaults.connection_type}
            if defaults.connection_type is not None
            else {}
        )
        inferred_filters = _filters_from_query(request.query)
        inferred_max_price = _max_price_from_query(request.query)
        filters = {**default_filters, **inferred_filters, **parsed.filters, **request.filters}
        unsupported_reason = _unsupported_reason(request.query)
        explicit_brands = request.brands or ([request.brand] if request.brand else [])
        excluded_brands = _dedupe_keep_order([*defaults.excluded_brands, *request.excluded_brands])
        requested_exclusions = {item.lower() for item in request.excluded_brands}
        inferred_brands = _infer_brands_from_text(request.query)
        selected_brands = explicit_brands or inferred_brands or defaults.brands
        selected_brands = [
            brand for brand in selected_brands if brand.lower() not in requested_exclusions
        ]
        excluded_brands = [
            brand
            for brand in excluded_brands
            if brand.lower() in requested_exclusions
            or brand.lower() not in {item.lower() for item in explicit_brands}
        ]
        excluded_usage = _dedupe_keep_order([*defaults.excluded_usage, *request.excluded_usage])
        inferred_usage = _usage_from_query(request.query)
        if inferred_usage in request.excluded_usage:
            inferred_usage = None
        usage = request.usage or inferred_usage
        if usage is None and defaults.usage not in excluded_usage:
            usage = defaults.usage
        if request.usage:
            excluded_usage = [item for item in excluded_usage if item != usage]
        return ProductQueryPlan(
            query=request.query,
            category=(
                request.category
                or parsed.category
                or _infer_category_from_text(request.query.lower())
            ),
            brands=selected_brands,
            excluded_brands=excluded_brands,
            excluded_usage=excluded_usage,
            min_price=request.min_price if request.min_price is not None else parsed.min_price,
            max_price=(
                request.max_price
                if request.max_price is not None
                else parsed.max_price
                if parsed.max_price is not None
                else inferred_max_price
                if inferred_max_price is not None
                else defaults.max_price
            ),
            filters=filters,
            keywords=_dedupe_keep_order([*request.keywords, *([usage] if usage else [])]),
            usage_scenario=usage,
            sort=_sort_from_query(request.query) or request.sort,
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
        explicit_brands = request.brands or ([request.brand] if request.brand else [])
        return await self._plan(
            task="search",
            query=request.query,
            limit=request.limit,
            overrides={
                "category": request.category,
                "brands": explicit_brands,
                "excluded_brands": request.excluded_brands,
                "excluded_usage": request.excluded_usage,
                "min_price": request.min_price,
                "max_price": request.max_price,
                "filters": request.filters,
                "keywords": request.keywords,
                "usage_scenario": request.usage,
                "sort": request.sort if request.sort != "recommend" else None,
            },
            preference_defaults=request.preference_defaults.model_dump(mode="json"),
        )

    async def plan_compare(self, request: CatalogCompareInput) -> CatalogComparePlan:
        payload = {
            "task": "compare",
            "query": request.query,
            "limit": request.limit,
        }
        plan_data = await self._invoke_with_retry(
            system_prompt=_catalog_compare_planner_system_prompt(),
            payload=payload,
            validator=_validate_catalog_compare_plan_data,
        )
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
        preference_defaults: dict,
    ) -> ProductQueryPlan:
        payload = {
            "task": task,
            "query": query,
            "limit": limit,
            "explicit_overrides": _json_safe(overrides),
            "preference_defaults": _json_safe(preference_defaults),
        }
        plan_data = await self._invoke_with_retry(
            system_prompt=_catalog_planner_system_prompt(),
            payload=payload,
            validator=_validate_product_plan_data,
        )
        plan_data["query"] = query
        plan_data["limit"] = limit
        _coerce_filter_values_to_strings(plan_data)
        plan = ProductQueryPlan.model_validate(plan_data)
        plan.planner = "llm"
        _apply_query_inferred_defaults(plan, query)
        _apply_preference_defaults(plan, preference_defaults)
        _apply_explicit_overrides(plan, overrides)
        return plan


    async def _invoke_with_retry(self, system_prompt: str, payload: dict, validator) -> dict:
        last_error: Exception | None = None
        retry_payload = dict(payload)
        for _attempt in range(2):
            response = await self.chat_model.ainvoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=json.dumps(retry_payload, ensure_ascii=False)),
                ]
            )
            try:
                plan_data = _extract_json_object(_message_content_to_text(response.content))
                validator(plan_data)
                return plan_data
            except (ValueError, ValidationError) as exc:
                last_error = exc
                retry_payload = {
                    **payload,
                    "validation_feedback": _retry_feedback(plan_data, exc),
                    "retry_instruction": (
                        "Return corrected JSON only. Use validation_feedback to fix "
                        "category, filter keys, and enum-like values."
                    ),
                }
        raise ValueError(f"LLM planner failed validation after retry: {last_error}")


class CatalogToolService:
    def __init__(
        self,
        session: AsyncSession,
        planner: CatalogQueryPlanner | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ):
        self.session = session
        self.planner = planner or RuleBasedCatalogQueryPlanner()
        self.session_factory = session_factory

    async def search(self, request: CatalogSearchInput) -> CatalogSearchOutput:
        requested_limit = _requested_product_count(request.query)
        if requested_limit is not None:
            request = request.model_copy(update={"limit": requested_limit})
        plan = await self._safe_plan_search(request)
        if plan.supported and plan.usage_scenario and not plan.category:
            return await self._search_usage_across_categories(request, plan)
        plan = _apply_usage_scenario_mapping(plan)
        if not plan.supported:
            diagnostics = _catalog_plan_diagnostics(plan, result_type="empty", count=0)
            return CatalogSearchOutput(
                result_type="empty",
                products=[],
                ranking_strategy="unsupported_query",
                query_plan=_plan_dump_with_diagnostics(plan, diagnostics),
                diagnostics=diagnostics,
            )

        product_request = _plan_to_product_search(plan)
        products = await CatalogRepository(self.session).search_products(product_request)
        products = _filter_brands(products, plan.brands)
        products = _filter_excluded_preferences(products, plan.excluded_brands, plan.excluded_usage)
        result_type = "products" if products else "empty"
        diagnostics = _catalog_plan_diagnostics(plan, result_type=result_type, count=len(products))
        return CatalogSearchOutput(
            result_type=result_type,
            products=products[: request.limit],
            ranking_strategy="match_score_sales_stock_price",
            query_plan=_plan_dump_with_diagnostics(plan, diagnostics),
            diagnostics=diagnostics,
        )

    async def _search_usage_across_categories(
        self,
        request: CatalogSearchInput,
        plan: ProductQueryPlan,
    ) -> CatalogSearchOutput:
        categories = USAGE_SCENARIO_CATEGORIES.get(plan.usage_scenario or "", ())
        if not categories:
            plan = _mark_usage_mapping_unavailable(plan)
            diagnostics = _catalog_plan_diagnostics(plan, result_type="empty", count=0)
            return CatalogSearchOutput(
                result_type="empty",
                products=[],
                ranking_strategy="unsupported_query",
                query_plan=_plan_dump_with_diagnostics(plan, diagnostics),
                diagnostics=diagnostics,
            )

        per_category_limit = min(20, max(2, request.limit))
        semaphore = asyncio.Semaphore(USAGE_CATEGORY_CONCURRENCY)

        async def search_category(
            category: str,
        ) -> tuple[str, ProductQueryPlan, list[ProductCard]]:
            category_plan = plan.model_copy(
                deep=True,
                update={"category": category, "limit": per_category_limit, "usage_mapping": {}},
            )
            category_plan = _apply_usage_scenario_mapping(category_plan)
            if not category_plan.supported:
                return category, category_plan, []

            async with semaphore:
                if self.session_factory is None:
                    products = await CatalogRepository(self.session).search_products(
                        _plan_to_product_search(category_plan)
                    )
                else:
                    async with self.session_factory() as category_session:
                        products = await CatalogRepository(category_session).search_products(
                            _plan_to_product_search(category_plan)
                        )
            products = _filter_brands(products, category_plan.brands)
            products = _filter_excluded_preferences(
                products,
                category_plan.excluded_brands,
                category_plan.excluded_usage,
            )
            return category, category_plan, products[:per_category_limit]

        if self.session_factory is None:
            category_results = []
            for category in categories:
                category_results.append(await search_category(category))
            execution = "sequential_shared_request_session"
        else:
            category_results = await asyncio.gather(
                *(search_category(category) for category in categories)
            )
            execution = "parallel_independent_sessions"

        products = _round_robin_category_products(category_results, limit=request.limit)
        plan.usage_mapping = {
            "status": "expanded",
            "source": "deterministic_spec_mapping",
            "rule_version": USAGE_MAPPING_VERSION,
            "scenario": plan.usage_scenario,
            "category": None,
            "categories": list(categories),
            "execution": execution,
            "category_rules": {
                category: category_plan.usage_mapping
                for category, category_plan, _products in category_results
            },
        }
        result_type = "products" if products else "empty"
        diagnostics = _catalog_plan_diagnostics(
            plan,
            result_type=result_type,
            count=len(products),
        )
        return CatalogSearchOutput(
            result_type=result_type,
            products=products,
            ranking_strategy="scenario_category_diversified_mapping",
            query_plan=_plan_dump_with_diagnostics(plan, diagnostics),
            diagnostics=diagnostics,
        )

    async def facets(self, request: CatalogFacetInput) -> CatalogFacetOutput:
        plan = _facet_query_plan(request)
        if not plan.supported:
            diagnostics = _facet_plan_diagnostics(plan, result_type="empty", count=0)
            return CatalogFacetOutput(
                result_type="empty",
                facet=plan.facet,
                items=[],
                category=plan.category,
                brand=plan.brand,
                spec_key=plan.spec_key,
                query_plan=_plan_dump_with_diagnostics(plan, diagnostics),
                diagnostics=diagnostics,
            )

        items = await CatalogRepository(self.session).list_facets(
            facet=plan.facet,
            category=plan.category,
            brand=plan.brand,
            spec_key=plan.spec_key,
            min_price=plan.min_price,
            max_price=plan.max_price,
            filters=plan.filters,
            limit=plan.limit,
        )
        result_type = "facets" if items else "empty"
        diagnostics = _facet_plan_diagnostics(plan, result_type=result_type, count=len(items))
        return CatalogFacetOutput(
            result_type=result_type,
            facet=plan.facet,
            items=[CatalogFacetItem(value=value, count=count) for value, count in items],
            category=plan.category,
            brand=plan.brand,
            spec_key=plan.spec_key,
            query_plan=_plan_dump_with_diagnostics(plan, diagnostics),
            diagnostics=diagnostics,
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
        result_type = "comparison" if products else "empty"
        diagnostics = _compare_diagnostics(
            compare_plan, result_type=result_type, count=len(products)
        )
        return CatalogCompareOutput(
            result_type=result_type,
            products=[_to_comparison_item(product) for product in products],
            comparison_fields=fields,
            missing_fields=_missing_fields(products, fields),
            query_plan={
                "mode": "direct_sku_ids" if request.sku_ids else "natural_language",
                "sku_ids": request.sku_ids,
                "query": request.query,
                "compare_plan": (
                    _plan_dump_with_diagnostics(compare_plan, diagnostics)
                    if compare_plan
                    else None
                ),
                "error_type": _diagnostic_error_type(diagnostics),
            },
            diagnostics=diagnostics,
        )

    async def _products_from_compare_query(
        self, request: CatalogCompareInput
    ) -> tuple[list[ProductCard], CatalogComparePlan]:
        plan = await self._safe_plan_compare(request)
        if not plan.supported:
            return [], plan
        products = await self._compare_candidates_from_plan(plan)
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
        matched = [product for product in ranked if _compare_term_score(product, wanted_terms) > 0]
        return matched or ranked, plan

    async def _compare_candidates_from_plan(
        self,
        plan: CatalogComparePlan,
    ) -> list[ProductCard]:
        if not plan.items:
            search_plan = _compare_plan_to_product_query_plan(plan)
            products = await CatalogRepository(self.session).search_products(
                _plan_to_product_search(search_plan)
            )
            return _filter_brands(products, plan.brands)

        candidates: list[ProductCard] = []
        seen_sku_ids: set[int] = set()
        per_item_limit = max(1, min(3, (plan.limit + len(plan.items) - 1) // len(plan.items)))
        for item in plan.items:
            item_brands = _brands_for_item(item, plan.brands) or plan.brands
            item_plan = ProductQueryPlan(
                query=item,
                category=plan.category,
                brands=item_brands,
                keywords=[item],
                limit=per_item_limit,
                supported=plan.supported,
                unsupported_reason=plan.unsupported_reason,
                planner=plan.planner,
                fallback_reason=plan.fallback_reason,
            )
            products = await CatalogRepository(self.session).search_products(
                _plan_to_product_search(item_plan)
            )
            products = _filter_brands(products, item_brands)
            ranked = sorted(
                products,
                key=lambda product: (
                    -_compare_term_score(product, [item]),
                    -product.sku_sales_count,
                    0 if product.stock > 0 else 1,
                    product.price,
                    product.title,
                ),
            )
            matched = [product for product in ranked if _compare_term_score(product, [item]) > 0]
            for product in (matched or ranked)[:per_item_limit]:
                if product.sku_id in seen_sku_ids:
                    continue
                candidates.append(product)
                seen_sku_ids.add(product.sku_id)
        return candidates

    async def _products_by_sku_ids(self, sku_ids: list[int]) -> list[ProductCard]:
        if not sku_ids:
            return []
        stmt = _active_sku_rows_statement(sku_ids)
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
                sku_sales_count=sku.sales_count,
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
            try:
                return validate_product_query_plan(fallback)
            except (ValidationError, ValueError, TypeError) as fallback_exc:
                fallback.supported = False
                fallback.unsupported_reason = str(fallback_exc)
                return fallback

    async def _safe_plan_compare(self, request: CatalogCompareInput) -> CatalogComparePlan:
        try:
            plan = validate_catalog_compare_plan(await self.planner.plan_compare(request))
            if plan.supported:
                return plan
            fallback = await RuleBasedCatalogQueryPlanner().plan_compare(request)
            if fallback.supported and (fallback.items or fallback.brands or fallback.category):
                fallback.planner = "rule_based_fallback"
                fallback.fallback_reason = plan.unsupported_reason or "llm_marked_unsupported"
                return validate_catalog_compare_plan(fallback)
            return plan
        except (ValidationError, ValueError, TypeError) as exc:
            fallback = await RuleBasedCatalogQueryPlanner().plan_compare(request)
            fallback.planner = "rule_based_fallback"
            fallback.fallback_reason = str(exc)
            return validate_catalog_compare_plan(fallback)


def _plan_dump_with_diagnostics(plan: BaseModel, diagnostics: list[ToolDiagnostic]) -> dict:
    data = plan.model_dump(mode="json")
    data["error_type"] = _diagnostic_error_type(diagnostics)
    return data


def _diagnostic_error_type(diagnostics: list[ToolDiagnostic]) -> str | None:
    for diagnostic in diagnostics:
        if diagnostic.code != "ok":
            return diagnostic.code
    return None


def _catalog_plan_diagnostics(
    plan: ProductQueryPlan,
    *,
    result_type: str,
    count: int,
) -> list[ToolDiagnostic]:
    if not plan.supported:
        if plan.usage_mapping.get("status") == "unavailable":
            return [
                ToolDiagnostic(
                    code="usage_mapping_unavailable",
                    severity="error",
                    message=plan.unsupported_reason
                    or "No deterministic specification mapping exists for this usage scenario.",
                    recommended_action="explain_limitation_and_ask_for_concrete_preferences",
                    details=plan.usage_mapping,
                )
            ]
        return [
            ToolDiagnostic(
                code="unsupported_query",
                severity="error",
                message=plan.unsupported_reason or "Catalog data does not support this query.",
                recommended_action="explain_unsupported_query",
                details={"unsupported_reason": plan.unsupported_reason},
            )
        ]
    if plan.fallback_reason:
        return [
            ToolDiagnostic(
                code="invalid_catalog_plan",
                severity="warning" if count else "error",
                message="LLM catalog planner failed validation; rule-based fallback was used.",
                recommended_action=(
                    "use_result_with_caution" if count else "ask_user_to_rephrase_or_relax_filters"
                ),
                details={"fallback_reason": plan.fallback_reason, "planner": plan.planner},
            )
        ]
    if result_type == "empty":
        return [
            ToolDiagnostic(
                code="empty_result",
                severity="info",
                message="Catalog query was valid but no products matched the filters.",
                recommended_action="relax_filters_or_ask_followup",
                details={
                    "filters": plan.filters,
                    "category": plan.category,
                    "brands": plan.brands,
                    "usage_mapping": plan.usage_mapping,
                },
            )
        ]
    if plan.normalization_debug:
        return [
            ToolDiagnostic(
                code="normalization_applied",
                severity="info",
                message="Catalog query terms were normalized before database lookup.",
                recommended_action="use_result",
                details=plan.normalization_debug,
            )
        ]
    return [
        ToolDiagnostic(
            code="ok",
            severity="info",
            message=(
                "Catalog query completed with a deterministic usage specification mapping."
                if plan.usage_mapping.get("status") in {"applied", "expanded"}
                else "Catalog query completed successfully."
            ),
            recommended_action="use_result",
            details=(plan.usage_mapping if plan.usage_mapping else {}),
        )
    ]


def _facet_plan_diagnostics(
    plan: FacetQueryPlan,
    *,
    result_type: str,
    count: int,
) -> list[ToolDiagnostic]:
    if not plan.supported:
        return [
            ToolDiagnostic(
                code="unsupported_query",
                severity="error",
                message=plan.unsupported_reason or "Catalog facet query is unsupported.",
                recommended_action="explain_unsupported_query",
                details={"unsupported_reason": plan.unsupported_reason},
            )
        ]
    if result_type == "empty":
        return [
            ToolDiagnostic(
                code="empty_result",
                severity="info",
                message="Facet query was valid but no values matched the filters.",
                recommended_action="relax_filters_or_ask_followup",
                details={"facet": plan.facet, "category": plan.category, "spec_key": plan.spec_key},
            )
        ]
    if plan.normalization_debug:
        return [
            ToolDiagnostic(
                code="normalization_applied",
                severity="info",
                message="Facet query terms were normalized before database lookup.",
                recommended_action="use_result",
                details=plan.normalization_debug,
            )
        ]
    return [
        ToolDiagnostic(
            code="ok",
            severity="info",
            message="Facet query completed successfully.",
            recommended_action="use_result",
            details={"count": count},
        )
    ]


def _compare_diagnostics(
    plan: CatalogComparePlan | None,
    *,
    result_type: str,
    count: int,
) -> list[ToolDiagnostic]:
    if plan and not plan.supported:
        return [
            ToolDiagnostic(
                code="unsupported_query",
                severity="error",
                message=plan.unsupported_reason or "Catalog comparison query is unsupported.",
                recommended_action="explain_unsupported_query",
                details={"unsupported_reason": plan.unsupported_reason},
            )
        ]
    if plan and plan.fallback_reason:
        return [
            ToolDiagnostic(
                code="invalid_catalog_plan",
                severity="warning" if count else "error",
                message="LLM comparison planner failed validation; rule-based fallback was used.",
                recommended_action=(
                    "use_result_with_caution" if count else "ask_user_to_rephrase_or_relax_filters"
                ),
                details={"fallback_reason": plan.fallback_reason, "planner": plan.planner},
            )
        ]
    if result_type == "empty":
        return [
            ToolDiagnostic(
                code="empty_result",
                severity="info",
                message="Comparison query was valid but no comparable products were found.",
                recommended_action="ask_user_for_specific_products_or_sku_ids",
            )
        ]
    if plan and plan.normalization_debug:
        return [
            ToolDiagnostic(
                code="normalization_applied",
                severity="info",
                message="Comparison query terms were normalized before database lookup.",
                recommended_action="use_result",
                details=plan.normalization_debug,
            )
        ]
    return [
        ToolDiagnostic(
            code="ok",
            severity="info",
            message="Comparison query completed successfully.",
            recommended_action="use_result",
        )
    ]


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


def _active_sku_rows_statement(sku_ids: list[int]):
    return (
        select(Sku, Spu, Brand, Category)
        .join(Spu, Sku.spu_id == Spu.id)
        .join(Brand, Spu.brand_id == Brand.id)
        .join(Category, Spu.category_id == Category.id)
        .where(Sku.id.in_(sku_ids), Sku.status == 1, Spu.status == 1)
    )


def validate_product_query_plan(plan: ProductQueryPlan | dict) -> ProductQueryPlan:
    if isinstance(plan, dict):
        plan = ProductQueryPlan.model_validate(plan)

    normalized_category = _canonical_category(plan.category)
    if normalized_category and normalized_category.lower() not in ALLOWED_CATEGORIES:
        raise ValueError(f"unsupported category: {plan.category}")
    plan.category = normalized_category

    if plan.usage_scenario:
        normalized_usage = _canonical_usage_scenario(plan.usage_scenario)
        if normalized_usage is None:
            raise ValueError(f"unsupported usage scenario: {plan.usage_scenario}")
        plan.usage_scenario = normalized_usage

    plan.filters, normalization_debug = _normalize_catalog_filters_with_debug(plan.filters)
    plan.filters, pruned_debug = _prune_redundant_filter_values(
        plan.filters, normalized_category
    )
    normalization_debug = {**normalization_debug, **pruned_debug}
    if normalization_debug:
        plan.normalization_debug = {**plan.normalization_debug, **normalization_debug}
    unknown_filters = {key for key in plan.filters if key.lower() not in ALLOWED_FILTERS}
    if unknown_filters:
        raise ValueError(f"unsupported catalog filters: {', '.join(sorted(unknown_filters))}")

    normalized_category = plan.category
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
    plan.brands = _normalize_brand_list(plan.brands)
    plan.keywords = [keyword.strip() for keyword in plan.keywords if keyword.strip()]
    return plan


def _normalize_catalog_filters(filters: dict[str, str]) -> dict[str, str]:
    return _normalize_catalog_filters_with_debug(filters)[0]


def _normalize_catalog_filters_with_debug(
    filters: dict[str, str],
) -> tuple[dict[str, str], dict[str, list[dict[str, dict[str, str]]]]]:
    normalized: dict[str, str] = {}
    changes: list[dict[str, dict[str, str]]] = []
    for raw_key, raw_value in filters.items():
        raw_key_text = str(raw_key)
        raw_value_text = str(raw_value)
        key = _normalize_filter_key(raw_key_text)
        value = _normalize_filter_value(key, raw_value_text)
        normalized[key] = value
        if key != raw_key_text or value != raw_value_text:
            changes.append(
                {
                    "from": {"key": raw_key_text, "value": raw_value_text},
                    "to": {"key": key, "value": value},
                }
            )
    debug = {"filter_aliases": changes} if changes else {}
    return normalized, debug


def _prune_redundant_filter_values(
    filters: dict[str, str], category: str | None
) -> tuple[dict[str, str], dict[str, list[dict[str, str]]]]:
    pruned = dict(filters)
    changes: list[dict[str, str]] = []
    if (
        category in {"headset", "headphone", "speaker"}
        and "type" in pruned
        and pruned["type"].strip().lower()
        in {"wireless", "wired", "bluetooth", "wifi", "usb", "usb-a", "usb-c"}
        and "connection_type" in pruned
    ):
        changes.append({"key": "type", "value": pruned.pop("type")})
    debug = {"pruned_filters": changes} if changes else {}
    return pruned, debug


def _normalize_filter_key(key: str) -> str:
    normalized = key.strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "connection": "connection_type",
        "connectivity": "connection_type",
        "wireless": "connection_type",
        "wired": "connection_type",
        "连接": "connection_type",
        "连接方式": "connection_type",
        "无线": "connection_type",
        "有线": "connection_type",
        "dpi": "max_dpi",
        "maxdpi": "max_dpi",
        "max_dpi": "max_dpi",
        "重量": "weight_g",
        "克重": "weight_g",
        "左右手": "hand_orientation",
        "手型": "hand_orientation",
        "传感器": "tracking_method",
        "追踪方式": "tracking_method",
        "分辨率": "resolution",
        "刷新率": "refresh_rate",
        "赫兹": "refresh_rate",
        "hz": "refresh_rate",
        "尺寸": "size_inch",
        "大小": "size_inch",
        "面板": "panel_type",
        "响应时间": "response_time_ms",
        "轴": "switches",
        "轴体": "switches",
        "背光": "backlit",
        "灯光": "backlit",
        "配列": "style",
        "布局": "style",
        "小键盘": "tenkeyless",
        "数字键盘": "tenkeyless",
        "外壳": "enclosure_type",
        "结构": "enclosure_type",
        "麦克风": "microphone",
        "麦": "microphone",
        "声道": "channels",
        "频响": "frequency_response",
        "频率响应": "frequency_response",
        "功率": "power_w",
        "瓦数": "power_w",
        "帧率": "frame_rate",
        "视场角": "field_of_view",
        "广角": "field_of_view",
        "颜色": "color",
        "类型": "type",
    }
    return aliases.get(normalized, normalized)


def _normalize_filter_value(key: str, value: str) -> str:
    stripped = value.strip()
    lowered = stripped.lower()
    if key == "connection_type":
        if lowered in {
            "true",
            "yes",
            "1",
            "wireless",
            "wifi",
            "bluetooth",
            "无线",
            "蓝牙",
            "三模",
            "2.4g",
            "2.4g 无线",
        }:
            return "Wireless"
        if lowered in {
            "false",
            "no",
            "0",
            "wired",
            "usb",
            "usb-a",
            "usb-c",
            "cable",
            "有线",
        }:
            return "Wired"
    if key == "wireless":
        if lowered in {"true", "yes", "1", "wireless", "是", "无线"}:
            return "true"
        if lowered in {"false", "no", "0", "wired", "否", "有线"}:
            return "false"
    if key == "color":
        color_aliases = {
            "black": "Black",
            "white": "White",
            "silver": "Silver",
            "gray": "Gray",
            "grey": "Gray",
            "pink": "Pink",
            "黑": "黑色",
            "黑色": "黑色",
            "白": "白色",
            "白色": "白色",
            "银": "银色",
            "银色": "银色",
            "灰": "灰色",
            "灰色": "灰色",
            "粉": "粉色",
            "粉色": "粉色",
        }
        return color_aliases.get(lowered, stripped)
    if key == "switches":
        switch_aliases = {
            "red": "Red",
            "blue": "Blue",
            "brown": "Brown",
            "magnetic": "Magnetic",
            "红": "红轴",
            "红轴": "红轴",
            "青": "青轴",
            "青轴": "青轴",
            "茶": "茶轴",
            "茶轴": "茶轴",
            "磁": "磁轴",
            "磁轴": "磁轴",
        }
        return switch_aliases.get(lowered, stripped)
    if key == "refresh_rate":
        if match := re.search(r"(\d{2,3})\s*(?:hz|赫兹)?", lowered):
            return f"{match.group(1)}Hz"
    if key == "frame_rate":
        if match := re.search(r"(\d{2,3})\s*(?:fps)?", lowered):
            return f"{match.group(1)}fps"
    if key == "power_w":
        if match := re.search(r"(\d{1,4})\s*(?:w|瓦)?", lowered):
            return match.group(1)
    if key == "resolution":
        resolution_aliases = {
            "2k": "2560x1440",
            "1440p": "2560x1440",
            "4k": "4K",
            "1080p": "1080p",
        }
        return resolution_aliases.get(lowered.replace(" ", ""), stripped)
    if key in {"microphone", "backlit"}:
        if lowered in {"true", "yes", "1", "有", "带", "是"}:
            return "Yes"
        if lowered in {"false", "no", "0", "无", "不带", "否"}:
            return "No"
    return stripped


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
    plan.brands = _normalize_brand_list(plan.brands)
    plan.comparison_fields = _dedupe_keep_order(
        [field.lower() for field in plan.comparison_fields if field.strip()]
    )
    return plan


def _canonical_usage_scenario(usage: str | None) -> str | None:
    if not usage:
        return None
    normalized = usage.strip().lower().replace("-", "_")
    if normalized in ALLOWED_USAGE_SCENARIOS:
        return normalized
    return USAGE_SCENARIO_ALIASES.get(normalized) or USAGE_SCENARIO_ALIASES.get(
        normalized.replace("_", " ")
    )


def _apply_usage_scenario_mapping(plan: ProductQueryPlan) -> ProductQueryPlan:
    if not plan.supported or not plan.usage_scenario:
        return plan

    category = _canonical_category(plan.category)
    rule = USAGE_SCENARIO_RULES.get((plan.usage_scenario, category or ""))
    if rule is None:
        return _mark_usage_mapping_unavailable(plan)

    required = list(rule.get("required", ()))
    preferred = list(rule.get("preferred", ()))
    plan.usage_mapping = {
        "status": "applied",
        "source": "deterministic_spec_mapping",
        "rule_version": USAGE_MAPPING_VERSION,
        "scenario": plan.usage_scenario,
        "category": category,
        "required": [condition.model_dump(mode="json") for condition in required],
        "preferred": [condition.model_dump(mode="json") for condition in preferred],
    }
    return plan


def _mark_usage_mapping_unavailable(plan: ProductQueryPlan) -> ProductQueryPlan:
    category = _canonical_category(plan.category)
    plan.supported = False
    plan.unsupported_reason = (
        f"No deterministic {plan.usage_scenario} specification mapping is configured "
        f"for category {category or 'unspecified'}."
    )
    plan.usage_mapping = {
        "status": "unavailable",
        "rule_version": USAGE_MAPPING_VERSION,
        "scenario": plan.usage_scenario,
        "category": category,
    }
    return plan


def _plan_to_product_search(plan: ProductQueryPlan) -> ProductSearchRequest:
    has_exclusions = bool(plan.excluded_brands or plan.excluded_usage)
    if has_exclusions:
        # Category, price and specs are already structured filters. In exclusion
        # queries, repeating localized category/usage words as title keywords can
        # eliminate every alternative before the post-retrieval exclusion pass.
        query_parts = [*plan.brands]
    elif plan.planner.startswith("rule_based") and _has_structured_catalog_constraints(plan):
        query_parts = [
            *plan.brands,
            *_product_keywords(plan),
            *_safe_query_prefilter_keywords(plan),
        ]
    elif plan.planner.startswith("rule_based"):
        query_parts = [plan.query, *plan.keywords]
    else:
        # Natural language is already represented by structured fields. Using model
        # keywords such as 144Hz, 2K or red switches as SQL title prefilters can
        # eliminate valid products before post-filtering specs.
        query_parts = [*plan.brands, *_safe_query_prefilter_keywords(plan)]
    usage_mapping_applied = plan.usage_mapping.get("status") == "applied"
    return ProductSearchRequest(
        query=" ".join(part for part in query_parts if part),
        category=plan.category,
        usage_scenario=None if usage_mapping_applied else plan.usage_scenario,
        usage_required_conditions=[
            ProductSpecCondition.model_validate(item)
            for item in plan.usage_mapping.get("required", [])
        ],
        usage_preferred_conditions=[
            ProductSpecCondition.model_validate(item)
            for item in plan.usage_mapping.get("preferred", [])
        ],
        min_price=plan.min_price,
        max_price=plan.max_price,
        filters=plan.filters,
        excluded_brands=plan.excluded_brands,
        excluded_usage=plan.excluded_usage,
        limit=min(20, max(12, plan.limit * 4)) if has_exclusions else plan.limit,
    )


def _has_structured_catalog_constraints(plan: ProductQueryPlan) -> bool:
    return bool(
        plan.category
        or plan.usage_scenario
        or plan.brands
        or plan.filters
        or plan.min_price is not None
        or plan.max_price is not None
    )


def _safe_query_prefilter_keywords(plan: ProductQueryPlan) -> list[str]:
    ignored = {
        "recommend",
        "recommendation",
        "find",
        "show",
        "with",
        "from",
        "under",
        "below",
        "within",
        "less",
        "than",
        "no",
        "more",
        "budget",
        "wireless",
        "wired",
        "bluetooth",
        "wifi",
        "usb",
        "usb-a",
        "usb-c",
        "switch",
        "switches",
        "red",
        "blue",
        "brown",
        "magnetic",
        "microphone",
        "mic",
        "backlit",
        "rgb",
        "2k",
        "4k",
        "1080p",
        "1440p",
        "fps",
        "gaming",
        "office",
        "video_meeting",
        "live_streaming",
        "meeting",
        "conference",
        "conferencing",
        "video",
        "zoom",
        "teams",
        "live",
        "stream",
        "streaming",
        "livestream",
        "mouse",
        "keyboard",
        "headset",
        "headphone",
        "monitor",
        "speaker",
        "webcam",
    }
    ignored.update(brand.lower() for brand in plan.brands)
    keywords: list[str] = []
    for token in re.findall(r"[a-z0-9][a-z0-9+.-]*", plan.query.lower()):
        if token in ignored or _is_spec_like_prefilter_token(token):
            continue
        if _canonical_category(token) in CATEGORY_FILTERS:
            continue
        if any(token in str(value).lower() for value in plan.filters.values()):
            continue
        keywords.append(token)
    return _dedupe_keep_order(keywords)


def _is_spec_like_prefilter_token(token: str) -> bool:
    if token.isdigit():
        return True
    return bool(
        re.fullmatch(
            r"\d+(?:\.\d+)?(?:hz|fps|w|k|p|ms|g|dpi|inch|in)", token
        )
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
        product for product in products if any(brand in product.brand.lower() for brand in lowered)
    ]


def _round_robin_category_products(
    category_results: list[tuple[str, ProductQueryPlan, list[ProductCard]]],
    *,
    limit: int,
) -> list[ProductCard]:
    selected: list[ProductCard] = []
    seen_sku_ids: set[int] = set()
    max_bucket_size = max((len(products) for _, _, products in category_results), default=0)
    for product_index in range(max_bucket_size):
        for _category, _plan, products in category_results:
            if product_index >= len(products):
                continue
            product = products[product_index]
            if product.sku_id in seen_sku_ids:
                continue
            selected.append(product)
            seen_sku_ids.add(product.sku_id)
            if len(selected) >= limit:
                return selected
    return selected


def _filters_from_query(query: str) -> dict[str, str]:
    lowered = query.lower()
    filters: dict[str, str] = {}
    if any(
        term in lowered
        for term in {"wireless", "wifi", "bluetooth", "无线", "蓝牙", "三模", "2.4g"}
    ):
        filters["connection_type"] = "Wireless"
    elif any(term in lowered for term in {"wired", "usb", "usb-a", "usb-c", "cable", "有线"}):
        filters["connection_type"] = "Wired"

    if color := _color_from_query(lowered):
        filters["color"] = color
    if match := re.search(r"(\d{2,3})\s*(?:hz|赫兹)", lowered):
        filters["refresh_rate"] = f"{match.group(1)}Hz"
    if match := re.search(r"(\d{2,3})\s*fps", lowered):
        filters["frame_rate"] = f"{match.group(1)}fps"
    if match := re.search(r"(\d{1,4})\s*w\b", lowered):
        filters["power_w"] = f"{match.group(1)}W"
    if any(term in lowered for term in {"2k", "1440p"}):
        filters["resolution"] = "2560x1440"
    elif "4k" in lowered:
        filters["resolution"] = "4K"
    elif "1080p" in lowered:
        filters["resolution"] = "1080p"

    if any(term in lowered for term in {"red switch", "red switches", "红轴"}):
        filters["switches"] = "Red"
    elif any(term in lowered for term in {"blue switch", "blue switches", "青轴"}):
        filters["switches"] = "Blue"
    elif any(term in lowered for term in {"brown switch", "brown switches", "茶轴"}):
        filters["switches"] = "Brown"
    elif any(term in lowered for term in {"magnetic switch", "magnetic switches", "磁轴"}):
        filters["switches"] = "Magnetic"

    if any(term in lowered for term in {"microphone", "mic", "麦克风", "带麦"}):
        filters["microphone"] = "Yes"
    if any(term in lowered for term in {"backlit", "rgb", "背光", "灯光"}):
        filters["backlit"] = "Yes"

    return filters


def _color_from_query(lowered_query: str) -> str | None:
    color_terms = (
        ("black", "Black"),
        ("white", "White"),
        ("silver", "Silver"),
        ("gray", "Gray"),
        ("grey", "Gray"),
        ("pink", "Pink"),
        ("黑色", "黑色"),
        ("白色", "白色"),
        ("银色", "银色"),
        ("灰色", "灰色"),
        ("粉色", "粉色"),
    )
    for term, color in color_terms:
        if term in lowered_query:
            return color
    return None


def _max_price_from_query(query: str) -> Decimal | None:
    lowered = query.lower().replace(",", "")
    patterns = (
        r"(?:under|below|within|less than|<=|no more than)\s*(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*(?:元|rmb|cny|usd|dollars?)?\s*(?:以内|以下|以下的|预算内)",
    )
    for pattern in patterns:
        if match := re.search(pattern, lowered):
            return Decimal(match.group(1))
    return None


def _infer_brands_from_text(query: str) -> list[str]:
    lowered = query.lower()
    brands = [brand for brand in KNOWN_BRANDS if brand.lower() in lowered]
    for alias, brand in BRAND_ALIASES.items():
        if alias.lower() in lowered:
            brands.append(brand)
    return _dedupe_keep_order(brands)


def _normalize_brand_list(brands: list[str]) -> list[str]:
    normalized = []
    for brand in brands:
        canonical = _canonical_brand(brand)
        if canonical:
            normalized.append(canonical)
    return _dedupe_keep_order(normalized)


def _canonical_brand(brand: str | None) -> str | None:
    if not brand:
        return None
    stripped = brand.strip()
    if not stripped:
        return None
    lowered = stripped.lower()
    for known_brand in KNOWN_BRANDS:
        if lowered == known_brand.lower():
            return known_brand
    return BRAND_ALIASES.get(stripped) or BRAND_ALIASES.get(lowered) or stripped


def _unsupported_reason(query: str) -> str | None:
    lowered = query.lower()
    for pattern, reason in UNSUPPORTED_QUERY_PATTERNS.items():
        if pattern in lowered:
            return reason
    return None


def _canonical_category(category: str | None) -> str | None:
    if not category:
        return None
    lowered = category.strip().lower()
    category_aliases = {
        "mouse": {
            "mouse",
            "mice",
            "鼠标",
            "游戏鼠标",
            str(CATEGORY_ALIASES.get("mouse", "")).lower(),
        },
        "keyboard": {
            "keyboard",
            "keyboards",
            "键盘",
            "机械键盘",
            str(CATEGORY_ALIASES.get("keyboard", "")).lower(),
        },
        "headset": {
            "headset",
            "headsets",
            "headphone",
            "headphones",
            "earphone",
            "earphones",
            "耳机",
            "耳麦",
            "头戴耳机",
            "游戏耳机",
            str(CATEGORY_ALIASES.get("headset", "")).lower(),
        },
        "monitor": {
            "monitor",
            "monitors",
            "display",
            "screen",
            "显示器",
            "屏幕",
            str(CATEGORY_ALIASES.get("monitor", "")).lower(),
        },
        "speaker": {
            "speaker",
            "speakers",
            "音箱",
            "音响",
            "蓝牙音箱",
            str(CATEGORY_ALIASES.get("speaker", "")).lower(),
        },
        "webcam": {
            "webcam",
            "webcams",
            "camera",
            "摄像头",
            "网络摄像头",
            str(CATEGORY_ALIASES.get("webcam", "")).lower(),
        },
    }
    for canonical, aliases in category_aliases.items():
        if lowered in {alias for alias in aliases if alias}:
            return canonical
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
        "video_meeting",
        "live_streaming",
        "meeting",
        "conference",
        "conferencing",
        "video",
        "zoom",
        "teams",
        "live",
        "stream",
        "streaming",
        "livestream",
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
- usage_scenario: one of office, gaming, video_meeting, live_streaming, or null
- sort: one of recommend, sales, price_asc, price_desc, stock
- supported: boolean
- unsupported_reason: string or null

Allowed filters:
backlit, channels, color, connection_type, enclosure_type, field_of_view,
frame_rate, frequency_response, hand_orientation, max_dpi, microphone,
panel_type, power_w, refresh_rate, resolution, response_time_ms, size_inch,
style, switches, tenkeyless, tracking_method, type, weight_g, wireless.

Filter value rules:
- Use connection_type for wired/wireless/bluetooth/USB intent; do not put wired or wireless in type.
- power_w must be a numeric string matching DB values, for example "20", "30",
  "40", "50", not "30W".
- frame_rate keeps fps suffix, for example "30fps" or "60fps"; refresh_rate keeps Hz suffix.
- resolution uses normalized values such as "1080p", "2560x1440", or "4K".
- microphone/backlit use "Yes" or "No".

Compact enum examples and aliases:
- category: mouse, keyboard, headset, monitor, speaker, webcam.
- usage_scenario aliases: 办公/office/码字/写代码=office;
  游戏/gaming/FPS/电竞/esports=gaming;
  开会/视频会议/远程会议/网课/video meeting/video conference=video_meeting;
  直播/主播/开播/live streaming/livestream=live_streaming.
  Never invent another usage_scenario value. Prefer the more specific scenario when several
  aliases occur: live_streaming, then video_meeting, then gaming, then office.
- brand aliases: 罗技=Logitech, 雷蛇=Razer, 赛睿=SteelSeries, 索尼=Sony,
  华硕=ASUS, 戴尔=Dell, 漫步者=Edifier, 博士=Bose, 圆刚=AVerMedia.
- connection_type: Wireless covers wireless, wifi, bluetooth, 蓝牙, 无线, 三模, 2.4G;
  Wired covers wired, cable, USB, USB-A, USB-C, 有线.
- color: Black/White/Silver/Gray/Pink or Chinese DB values 黑色/白色/银色/灰色/粉色.
- switches: Red/Blue/Brown/Magnetic or Chinese DB values 红轴/青轴/茶轴/磁轴.
- monitor resolution: 1080p, 2560x1440, 4K; refresh_rate examples: 75Hz, 144Hz, 165Hz, 240Hz.
- webcam frame_rate examples: 30fps, 60fps.
- speaker power_w examples: "20", "30", "40", "50".

Set supported=false when the user asks for analytics not available in the
catalog tables, such as time-series growth, revenue, profit, or user purchase
statistics. Otherwise set supported=true.

The current query and explicit_overrides always take precedence.
preference_defaults only fill fields that the current request leaves unspecified.
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
price, stock, brand, category, sku_sales_count, sales_count, backlit, channels, color,
connection_type, enclosure_type, field_of_view, frame_rate, frequency_response,
hand_orientation, max_dpi, microphone, panel_type, power_w, refresh_rate,
resolution, response_time_ms, size_inch, style, switches, tenkeyless,
tracking_method, type, weight_g, wireless.

Use connection_type for wired/wireless/bluetooth/USB facts. Do not use type for connection mode.
power_w is stored as a numeric string such as "20", "30", "40", "50", not "30W".
Prefer these enum-style values when relevant: connection_type Wireless/Wired; microphone Yes/No;
resolution 1080p/2560x1440/4K; frame_rate 30fps/60fps; switches
Red/Blue/Brown/Magnetic or 红轴/青轴/茶轴/磁轴.

sku_sales_count is SKU-level sales volume. sales_count is SPU-level aggregate sales volume.
Do not compare color/version popularity with sales_count; use sku_sales_count for SKU popularity.

For FPS mouse comparisons, prefer fields: price, stock, sku_sales_count, sales_count,
connection_type, max_dpi, weight_g, hand_orientation.

Set supported=false when the user asks for analytics not available in catalog
tables, such as time-series growth, revenue, profit, or user purchase statistics.
""".strip()


def _retry_feedback(plan_data: dict, exc: Exception) -> dict:
    category = _canonical_category(str(plan_data.get("category") or ""))
    allowed_for_category = (
        sorted(CATEGORY_FILTERS[category])
        if category and category in CATEGORY_FILTERS
        else None
    )
    return {
        "error": str(exc),
        "received_category": plan_data.get("category"),
        "received_filters": plan_data.get("filters") if isinstance(plan_data, dict) else None,
        "allowed_categories": sorted(CATEGORY_FILTERS),
        "allowed_filters": sorted(ALLOWED_FILTERS),
        "allowed_filters_for_received_category": allowed_for_category,
        "normalization_hints": _normalization_hints(),
    }


def _normalization_hints() -> dict[str, dict[str, str]]:
    return {
        "bluetooth": {"key": "connection_type", "value": "Wireless"},
        "wifi": {"key": "connection_type", "value": "Wireless"},
        "wireless": {"key": "connection_type", "value": "Wireless"},
        "蓝牙": {"key": "connection_type", "value": "Wireless"},
        "无线": {"key": "connection_type", "value": "Wireless"},
        "三模": {"key": "connection_type", "value": "Wireless"},
        "2.4G": {"key": "connection_type", "value": "Wireless"},
        "wired": {"key": "connection_type", "value": "Wired"},
        "USB-C": {"key": "connection_type", "value": "Wired"},
        "USB-A": {"key": "connection_type", "value": "Wired"},
        "有线": {"key": "connection_type", "value": "Wired"},
        "30W": {"key": "power_w", "value": "30"},
        "20W": {"key": "power_w", "value": "20"},
        "red switch": {"key": "switches", "value": "Red"},
        "红轴": {"key": "switches", "value": "红轴"},
        "2K": {"key": "resolution", "value": "2560x1440"},
        "144Hz": {"key": "refresh_rate", "value": "144Hz"},
        "60fps": {"key": "frame_rate", "value": "60fps"},
        "with microphone": {"key": "microphone", "value": "Yes"},
    }


def _validate_product_plan_data(plan_data: dict) -> None:
    probe = dict(plan_data)
    probe.setdefault("query", "validation probe")
    probe.setdefault("limit", 3)
    _coerce_filter_values_to_strings(probe)
    validate_product_query_plan(ProductQueryPlan.model_validate(probe))


def _validate_catalog_compare_plan_data(plan_data: dict) -> None:
    probe = dict(plan_data)
    probe.setdefault("query", "validation probe")
    probe.setdefault("limit", 5)
    validate_catalog_compare_plan(CatalogComparePlan.model_validate(probe))


def _coerce_filter_values_to_strings(plan_data: dict) -> None:
    filters = plan_data.get("filters")
    if not isinstance(filters, dict):
        return
    plan_data["filters"] = {str(key): str(value) for key, value in filters.items()}


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


def _sort_from_query(
    query: str,
) -> Literal["recommend", "sales", "price_asc", "price_desc", "stock"] | None:
    lowered = query.casefold().replace(" ", "")
    if any(
        marker in lowered
        for marker in (
            "销量最高",
            "销量最好",
            "销量排行",
            "按销量",
            "最畅销",
            "最热销",
            "bestselling",
            "topselling",
        )
    ):
        return "sales"
    return None


def _requested_product_count(query: str) -> int | None:
    normalized = query.casefold().replace("，", " ").replace(",", " ")
    digit_match = re.search(
        r"(?:前\s*|top\s*)?(\d{1,2})\s*(?:款|个|台|种|件|名|products?|items?|models?)",
        normalized,
    )
    if digit_match:
        value = int(digit_match.group(1))
        return value if 1 <= value <= 20 else None

    chinese_match = re.search(
        r"(?:前\s*)?([一二两三四五六七八九十]+)\s*(?:款|个|台|种|件|名)",
        normalized,
    )
    if not chinese_match:
        return None
    value = _parse_small_chinese_number(chinese_match.group(1))
    return value if value is not None and 1 <= value <= 20 else None


def _parse_small_chinese_number(value: str) -> int | None:
    digits = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value in digits:
        return digits[value]
    if value == "十":
        return 10
    if "十" not in value or value.count("十") != 1:
        return None
    tens, ones = value.split("十", maxsplit=1)
    tens_value = digits.get(tens, 1) if tens else 1
    ones_value = digits.get(ones, 0) if ones else 0
    return tens_value * 10 + ones_value


def _apply_query_inferred_defaults(plan: ProductQueryPlan, query: str) -> None:
    inferred_category = _infer_category_from_text(query.lower())
    if not plan.category and inferred_category:
        plan.category = inferred_category
    if not plan.brands:
        plan.brands = _infer_brands_from_text(query)
    inferred_max_price = _max_price_from_query(query)
    if plan.max_price is None and inferred_max_price is not None:
        plan.max_price = inferred_max_price
    inferred_filters = _filters_from_query(query)
    for key, value in inferred_filters.items():
        plan.filters.setdefault(key, value)
    if plan.usage_scenario is None:
        plan.usage_scenario = _usage_from_query(query)
    inferred_sort = _sort_from_query(query)
    if inferred_sort is not None:
        plan.sort = inferred_sort


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
    if overrides.get("keywords"):
        plan.keywords = list(overrides["keywords"])
    if overrides.get("sort"):
        plan.sort = overrides["sort"]
    if overrides.get("excluded_brands"):
        plan.excluded_brands = _dedupe_keep_order(
            [*plan.excluded_brands, *overrides["excluded_brands"]]
        )
        excluded = {item.lower() for item in overrides["excluded_brands"]}
        plan.brands = [item for item in plan.brands if item.lower() not in excluded]
    if overrides.get("excluded_usage"):
        plan.excluded_usage = _dedupe_keep_order(
            [*plan.excluded_usage, *overrides["excluded_usage"]]
        )
        if plan.usage_scenario in overrides["excluded_usage"]:
            plan.usage_scenario = None
            plan.keywords = [
                item for item in plan.keywords if item not in overrides["excluded_usage"]
            ]
    if overrides.get("brands") and not overrides.get("excluded_brands"):
        included = {item.lower() for item in plan.brands}
        plan.excluded_brands = [
            item for item in plan.excluded_brands if item.lower() not in included
        ]
    if overrides.get("usage_scenario"):
        plan.usage_scenario = overrides["usage_scenario"]
        plan.keywords = _dedupe_keep_order([*plan.keywords, str(overrides["usage_scenario"])])


def _apply_preference_defaults(plan: ProductQueryPlan, defaults: dict) -> None:
    if not plan.brands and defaults.get("brands"):
        plan.brands = list(defaults["brands"])
    plan.excluded_brands = _dedupe_keep_order(
        [*plan.excluded_brands, *defaults.get("excluded_brands", [])]
    )
    plan.excluded_usage = _dedupe_keep_order(
        [*plan.excluded_usage, *defaults.get("excluded_usage", [])]
    )
    excluded_brands = {item.lower() for item in plan.excluded_brands}
    plan.brands = [item for item in plan.brands if item.lower() not in excluded_brands]
    if plan.usage_scenario in plan.excluded_usage:
        plan.usage_scenario = None
        plan.keywords = [item for item in plan.keywords if item not in plan.excluded_usage]
    if plan.max_price is None and defaults.get("max_price") is not None:
        plan.max_price = defaults["max_price"]
    connection_type = defaults.get("connection_type")
    if connection_type and "connection_type" not in plan.filters:
        plan.filters["connection_type"] = connection_type
    usage = defaults.get("usage")
    if plan.usage_scenario is None and usage:
        plan.usage_scenario = str(usage)
        plan.keywords = _dedupe_keep_order([*plan.keywords, str(usage)])


def _json_safe(data: dict) -> dict:
    return json.loads(json.dumps(data, default=str))


def _usage_from_query(query: str) -> str | None:
    lowered = query.lower()
    if _contains_any_usage_term(
        lowered,
        {"live streaming", "livestream", "直播", "主播", "开播"},
    ):
        return "live_streaming"
    if _contains_any_usage_term(
        lowered,
        {
            "video meeting",
            "video conference",
            "video conferencing",
            "zoom",
            "teams",
            "开会",
            "会议",
            "网课",
            "视频通话",
        },
    ):
        return "video_meeting"
    if _contains_any_usage_term(
        lowered,
        {"fps", "game", "gaming", "esports", "游戏", "电竞"},
    ):
        return "gaming"
    if _contains_any_usage_term(
        lowered,
        {"office", "办公", "码字", "写代码", "生产力", "学习"},
    ):
        return "office"
    return None


def _contains_any_usage_term(query: str, terms: set[str]) -> bool:
    for term in terms:
        if term.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", query):
                return True
        elif term in query:
            return True
    return False


def _filter_excluded_preferences(
    products: list[ProductCard], excluded_brands: list[str], excluded_usage: list[str]
) -> list[ProductCard]:
    brand_terms = {item.lower() for item in excluded_brands}
    usage_terms = {
        term
        for usage in excluded_usage
        for term in {
            usage.lower(),
            "游戏" if usage.lower() == "gaming" else "办公" if usage.lower() == "office" else "",
        }
        if term
    }

    def allowed(product: ProductCard) -> bool:
        if product.brand.lower() in brand_terms:
            return False
        haystack = " ".join(
            [product.title, product.category, *[str(value) for value in product.specs.values()]]
        ).lower()
        return not any(term in haystack for term in usage_terms)

    return [product for product in products if allowed(product)]


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
    if any(term in lowered for term in {"sales", "sales count", "销量"}):
        fields.extend(["sku_sales_count", "sales_count"])
    if "fps" in lowered:
        fields.extend(
            [
                "price",
                "stock",
                "sku_sales_count",
                "sales_count",
                "connection_type",
                "max_dpi",
                "weight_g",
            ]
        )
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


def _brands_for_item(item: str, brands: list[str]) -> list[str]:
    item_lower = item.lower()
    return [brand for brand in brands if brand.lower() in item_lower]


def _facet_query_plan(request: CatalogFacetInput) -> FacetQueryPlan:
    normalized_request = _normalize_facet_request(request)
    filters, normalization_debug = _normalize_catalog_filters_with_debug(normalized_request.filters)
    spec_key = (
        _normalize_filter_key(normalized_request.spec_key) if normalized_request.spec_key else None
    )
    category = _canonical_category(normalized_request.category)
    plan = FacetQueryPlan(
        query=normalized_request.query,
        facet=normalized_request.facet,
        category=category,
        brand=normalized_request.brand,
        spec_key=spec_key,
        min_price=normalized_request.min_price,
        max_price=normalized_request.max_price,
        filters={key.lower(): str(value) for key, value in filters.items()},
        limit=normalized_request.limit,
        normalization_debug=normalization_debug,
    )

    if category and category not in ALLOWED_CATEGORIES:
        plan.supported = False
        plan.unsupported_reason = f"unsupported category: {category}"
        return plan

    unknown_filters = {key for key in plan.filters if key.lower() not in ALLOWED_FILTERS}
    if unknown_filters:
        plan.supported = False
        plan.unsupported_reason = "unsupported catalog filters: " + ", ".join(
            sorted(unknown_filters)
        )
        return plan

    if spec_key and spec_key.lower() not in ALLOWED_FILTERS:
        plan.supported = False
        plan.unsupported_reason = f"unsupported spec_key: {spec_key}"
        return plan

    if plan.facet == "spec_value" and not spec_key:
        inferred_spec_key = _infer_spec_key_from_text(plan.query)
        if inferred_spec_key:
            spec_key = inferred_spec_key
            plan.spec_key = inferred_spec_key
        else:
            plan.supported = False
            plan.unsupported_reason = "spec_value facet requires spec_key"
            return plan

    if category and category in CATEGORY_FILTERS:
        allowed = CATEGORY_FILTERS[category]
        disallowed_filters = {key for key in plan.filters if key.lower() not in allowed}
        if disallowed_filters:
            plan.supported = False
            plan.unsupported_reason = f"unsupported filters for {category}: " + ", ".join(
                sorted(disallowed_filters)
            )
            return plan
        if spec_key and spec_key.lower() not in allowed:
            plan.supported = False
            plan.unsupported_reason = f"unsupported spec_key for {category}: {spec_key}"
            return plan

    if plan.min_price is not None and plan.min_price < 0:
        plan.supported = False
        plan.unsupported_reason = "min_price cannot be negative"
        return plan
    if plan.max_price is not None and plan.max_price < 0:
        plan.supported = False
        plan.unsupported_reason = "max_price cannot be negative"
        return plan
    if (
        plan.min_price is not None
        and plan.max_price is not None
        and plan.min_price > plan.max_price
    ):
        plan.supported = False
        plan.unsupported_reason = "min_price cannot be greater than max_price"
        return plan

    if reason := _unsupported_reason(plan.query):
        plan.supported = False
        plan.unsupported_reason = reason

    return plan


def _normalize_facet_request(request: CatalogFacetInput) -> CatalogFacetInput:
    data = request.model_dump(mode="python")
    query = request.query.lower()
    inferred_facet = _infer_facet_from_text(query)
    if inferred_facet:
        data["facet"] = inferred_facet
    if not request.category:
        if category := _infer_category_from_text(query):
            data["category"] = category
    if not request.brand:
        if brand := _infer_brand_from_text(query):
            data["brand"] = brand
    if not request.spec_key:
        if spec_key := _infer_spec_key_from_text(query):
            data["spec_key"] = spec_key
            if inferred_facet is None and request.facet == "brand":
                data["facet"] = "spec_value"
    if data.get("category") == "speaker" and _looks_like_power_facet_query(query):
        data["facet"] = "spec_value"
        data["spec_key"] = "power_w"
    return CatalogFacetInput.model_validate(data)


def _infer_facet_from_text(query: str) -> str | None:
    if _asks_for_spec_values(query):
        return "spec_value"
    if _asks_for_spec_keys(query):
        return "spec_key"
    if _asks_for_categories(query):
        return "category"
    if _asks_for_brands(query):
        return "brand"
    return None


def _asks_for_brands(query: str) -> bool:
    brand_terms = {
        "brand",
        "brands",
        "maker",
        "manufacturers",
        "牌子",
        "品牌",
    }
    return any(term in query for term in brand_terms)


def _asks_for_categories(query: str) -> bool:
    category_terms = {
        "category",
        "categories",
        "type",
        "types",
        "product line",
        "peripheral",
        "peripherals",
        "品类",
        "类目",
        "类型",
        "外设",
    }
    return any(term in query for term in category_terms) and not _asks_for_spec_values(query)


def _asks_for_spec_keys(query: str) -> bool:
    spec_key_terms = {
        "spec",
        "specs",
        "specification",
        "specifications",
        "parameter",
        "parameters",
        "filter",
        "filters",
        "规格",
        "参数",
        "筛选",
    }
    return any(term in query for term in spec_key_terms) and not _asks_for_spec_values(query)


def _asks_for_spec_values(query: str) -> bool:
    value_terms = {
        "available",
        "values",
        "options",
        "哪些",
        "可选",
        "有什么",
        "有哪",
    }
    return _infer_spec_key_from_text(query) is not None and any(
        term in query for term in value_terms
    )


def _looks_like_power_facet_query(query: str) -> bool:
    power_terms = {"power", "watt", "wattage", "功率", "瓦数", "多少w", "多少瓦"}
    value_terms = {"available", "values", "options", "哪些", "档位", "可选", "有什么", "有哪"}
    return any(term in query for term in power_terms) and any(term in query for term in value_terms)


def _infer_category_from_text(query: str) -> str | None:
    for term in CATEGORY_FILTERS:
        if term in query:
            return term
    for raw in sorted(CATEGORY_ALIASES, key=len, reverse=True):
        if raw.lower() in query:
            return _canonical_category(raw)
    return None


BRAND_ALIASES = {
    "罗技": "Logitech",
    "logi": "Logitech",
    "雷蛇": "Razer",
    "赛睿": "SteelSeries",
    "steelseries": "SteelSeries",
    "脉冲星": "Pulsar",
    "凯酷": "Keychron",
    "键盘侠": "Keychron",
    "艾酷": "Akko",
    "艾石头": "Akko",
    "wooting": "Wooting",
    "极度未知": "HyperX",
    "金士顿": "HyperX",
    "索尼": "Sony",
    "冠捷": "AOC",
    "华硕": "ASUS",
    "戴尔": "Dell",
    "乐金": "LG",
    "漫步者": "Edifier",
    "博士": "Bose",
    "bose": "Bose",
    "创新": "Creative",
    "爱乐图": "Elgato",
    "圆刚": "AVerMedia",
    "圆展": "AVerMedia",
}


KNOWN_BRANDS = (
    "Logitech",
    "Razer",
    "SteelSeries",
    "Pulsar",
    "Keychron",
    "Akko",
    "Wooting",
    "HyperX",
    "Sony",
    "AOC",
    "ASUS",
    "Dell",
    "LG",
    "Edifier",
    "JBL",
    "Bose",
    "Creative",
    "Elgato",
    "AVerMedia",
)


def _infer_brand_from_text(query: str) -> str | None:
    brands = _infer_brands_from_text(query)
    return brands[0] if brands else None


def _infer_spec_key_from_text(query: str) -> str | None:
    aliases = {
        "refresh_rate": {"refresh", "hz", "刷新率"},
        "resolution": {"resolution", "2k", "4k", "分辨率"},
        "switches": {"switch", "switches", "axis", "轴", "轴体"},
        "connection_type": {
            "wireless",
            "wired",
            "connection",
            "无线",
            "有线",
            "连接",
        },
        "max_dpi": {"dpi"},
        "color": {"color", "colour", "颜色"},
    }
    for key, terms in aliases.items():
        if any(term in query for term in terms):
            return key
    return None
