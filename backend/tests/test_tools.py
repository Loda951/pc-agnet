import asyncio
import re
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

import app.tools.catalog as catalog_tools
from app.core.config import Settings
from app.models import Sku, Spu
from app.schemas.catalog import ProductCard
from app.tools.catalog import (
    CatalogComparePlan,
    CatalogToolService,
    LLMCatalogQueryPlanner,
    ProductQueryPlan,
    validate_catalog_sql,
    validate_product_query_plan,
)
from app.tools.contracts import (
    BoundTool,
    DefaultToolContractProvider,
    RegistryToolExecutor,
    ToolCatalog,
    build_catalog_planner,
)
from app.tools.knowledge import (
    KnowledgeRetrievalToolService,
    KnowledgeVectorIndex,
    KnowledgeVectorIndexChunk,
)
from app.tools.registry import build_tool_registry
from app.tools.schemas import (
    CatalogCompareInput,
    CatalogSearchInput,
    CatalogSearchOutput,
    DocumentSearchInput,
)

TOOL_TEST_SETTINGS = Settings(llm_api_key="", catalog_llm_planner_enabled=False)


def test_catalog_exclusions_filter_brand_and_usage_matches() -> None:
    products = [
        ProductCard(
            spu_id=1,
            sku_id=11,
            title="Logitech Office Mouse",
            brand="Logitech",
            category="mouse",
            price="199.00",
            stock=5,
            sales_count=10,
            specs={"usage": "office"},
        ),
        ProductCard(
            spu_id=2,
            sku_id=22,
            title="Razer Gaming Mouse",
            brand="Razer",
            category="mouse",
            price="299.00",
            stock=5,
            sales_count=20,
            specs={"usage": "gaming"},
        ),
    ]

    assert [
        item.sku_id
        for item in catalog_tools._filter_excluded_preferences(products, ["Logitech"], [])
    ] == [
        22
    ]
    assert [
        item.sku_id
        for item in catalog_tools._filter_excluded_preferences(products, [], ["gaming"])
    ] == [
        11
    ]


@pytest.mark.asyncio
async def test_negative_brand_search_recalls_alternatives_before_filtering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    products = [
        ProductCard(
            spu_id=1,
            sku_id=11,
            title="Logitech Mouse",
            brand="Logitech",
            category="mouse",
            price="199.00",
            stock=5,
            sales_count=10,
            specs={},
        ),
        ProductCard(
            spu_id=2,
            sku_id=22,
            title="Razer Mouse",
            brand="Razer",
            category="mouse",
            price="299.00",
            stock=5,
            sales_count=20,
            specs={},
        ),
    ]
    captured = {}

    class FakeCatalogRepository:
        def __init__(self, session):
            pass

        async def search_products(self, request):
            captured["request"] = request
            if request.query:
                return []
            return products

    monkeypatch.setattr(catalog_tools, "CatalogRepository", FakeCatalogRepository)
    service = CatalogToolService(SimpleNamespace())

    result = await service.search(
        CatalogSearchInput(
            query="不要 Logitech 游戏鼠标",
            category="鼠标",
            excluded_brands=["Logitech"],
            excluded_usage=["gaming"],
            limit=3,
        )
    )

    assert captured["request"].query == ""
    assert captured["request"].limit >= 12
    assert captured["request"].excluded_brands == ["Logitech"]
    assert captured["request"].excluded_usage == ["gaming"]
    assert [item.brand for item in result.products] == ["Razer"]


class FakeEmbeddingProvider:
    model_name = "fake-embedding"

    def embed_query(self, text: str) -> list[float]:
        lowered = text.lower()
        if "wooting" in lowered or "brand" in lowered:
            return [1.0, 0.0]
        if "return" in lowered or "refund" in lowered:
            return [0.0, 1.0]
        return [0.5, 0.5]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]


def _fake_vector_index() -> KnowledgeVectorIndex:
    return KnowledgeVectorIndex(
        version=1,
        embedding_provider="sentence_transformers",
        embedding_model=FakeEmbeddingProvider.model_name,
        documents_hash="test-documents",
        chunk_size=420,
        chunk_overlap=80,
        query_instruction="",
        chunks=[
            KnowledgeVectorIndexChunk(
                document_id=1,
                chunk_id="1:0",
                text="return refund after sales",
                embedding=[0.0, 1.0],
            ),
            KnowledgeVectorIndexChunk(
                document_id=5,
                chunk_id="5:0",
                text="Logitech Razer Wooting brand",
                embedding=[1.0, 0.0],
            ),
        ],
    )


def _knowledge_service() -> KnowledgeRetrievalToolService:
    return KnowledgeRetrievalToolService(
        embedding_provider=FakeEmbeddingProvider(),
        vector_index=_fake_vector_index(),
    )


class FakeCatalogPlanner:
    async def plan_search(self, request):
        return ProductQueryPlan(
            query=request.query,
            category="mouse",
            brands=["Razer"],
            filters={"connection_type": "wireless"},
            keywords=["Viper"],
            limit=request.limit,
            planner="fake",
        )

    async def plan_compare(self, request):
        return CatalogComparePlan(
            query=request.query,
            category="mouse",
            items=["G502", "Viper"],
            comparison_fields=["price", "stock", "max_dpi", "weight_g", "connection_type"],
            limit=request.limit,
            planner="fake",
        )


class BrokenCatalogPlanner:
    async def plan_search(self, request):
        raise ValueError("planner unavailable")

    async def plan_compare(self, request):
        raise ValueError("planner unavailable")


class FakeChatResponse:
    def __init__(self, content: str):
        self.content = content


class FakeChatModel:
    def __init__(self, content: str):
        self.content = content
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return FakeChatResponse(self.content)


@pytest.mark.asyncio
async def test_tool_registry_exposes_expected_business_tools(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        registry = build_tool_registry(session, settings=TOOL_TEST_SETTINGS)

    assert registry.tool_names == [
        "catalog.compare",
        "catalog.facets",
        "catalog.search",
        "knowledge.search",
        "order.lookup",
        "policy.search",
    ]


def test_tool_contracts_expose_llm_safe_metadata() -> None:
    contracts = DefaultToolContractProvider().list_contracts()
    names = [contract.llm_name for contract in contracts]

    assert names == [
        "catalog_search",
        "catalog_compare",
        "catalog_facets",
        "order_lookup",
        "policy_search",
        "knowledge_search",
    ]
    assert len(set(names)) == len(names)
    assert all(re.fullmatch(r"[a-zA-Z0-9_]+", name) for name in names)
    assert all(contract.description for contract in contracts)
    assert all(contract.read_only for contract in contracts)
    assert not any(contract.parallel_safe for contract in contracts)

    order_contract = DefaultToolContractProvider().get_contract("order_lookup")
    assert order_contract is not None
    assert order_contract.requires_auth is True
    assert order_contract.runtime_fields == ("user_id",)
    assert order_contract.public_input_model.model_json_schema()["additionalProperties"] is False
    assert "user_id" not in order_contract.public_input_model.model_json_schema()["properties"]


def test_tool_contracts_export_public_llm_schemas_only() -> None:
    contracts = DefaultToolContractProvider().list_contracts()

    for contract in contracts:
        schema = contract.as_llm_tool()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == contract.llm_name
        assert schema["function"]["description"] == contract.description
        parameters = schema["function"]["parameters"]
        for runtime_field in contract.runtime_fields:
            assert runtime_field not in parameters.get("properties", {})


@pytest.mark.asyncio
async def test_tool_contracts_align_with_registry(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        registry = build_tool_registry(session, settings=TOOL_TEST_SETTINGS)

    contracts = DefaultToolContractProvider().list_contracts()
    assert sorted(contract.registry_name for contract in contracts) == registry.tool_names


def test_all_public_input_models_forbid_unknown_fields() -> None:
    contracts = DefaultToolContractProvider().list_contracts()

    for contract in contracts:
        schema = contract.public_input_model.model_json_schema()
        assert schema.get("additionalProperties") is False


def test_catalog_public_schemas_are_query_first() -> None:
    provider = DefaultToolContractProvider()
    search = provider.get_contract("catalog_search")
    facets = provider.get_contract("catalog_facets")
    assert search is not None
    assert facets is not None

    search_fields = set(search.public_input_model.model_json_schema()["properties"])
    facet_fields = set(facets.public_input_model.model_json_schema()["properties"])

    assert search_fields == {"query", "limit"}
    assert facet_fields == {"query", "limit"}
    assert search.internal_input_model is CatalogSearchInput


def test_tool_catalog_rejects_duplicate_names_and_missing_handler() -> None:
    contract = DefaultToolContractProvider().get_contract("catalog_search")
    assert contract is not None

    async def handler(request: CatalogSearchInput) -> CatalogSearchOutput:
        return CatalogSearchOutput(result_type="empty")

    with pytest.raises(ValueError, match="duplicate tool llm_name"):
        ToolCatalog([BoundTool(contract, handler), BoundTool(contract, handler)])

    with pytest.raises(ValueError, match="missing handler"):
        ToolCatalog([BoundTool(contract, None)])  # type: ignore[arg-type]


def test_tool_catalog_rejects_handler_model_mismatch() -> None:
    contract = DefaultToolContractProvider().get_contract("catalog_search")
    assert contract is not None

    async def wrong_input_handler(request: DocumentSearchInput) -> CatalogSearchOutput:
        return CatalogSearchOutput(result_type="empty")

    async def wrong_output_handler(request: CatalogSearchInput) -> BaseModel:
        return CatalogSearchOutput(result_type="empty")

    with pytest.raises(ValueError, match="handler input model mismatch"):
        ToolCatalog([BoundTool(contract, wrong_input_handler)])

    with pytest.raises(ValueError, match="handler output model mismatch"):
        ToolCatalog([BoundTool(contract, wrong_output_handler)])


@pytest.mark.asyncio
async def test_registry_rejects_unknown_fields_without_calling_handler() -> None:
    async def never_called_handler(request):  # pragma: no cover
        raise AssertionError("handler should not be called")

    for contract in DefaultToolContractProvider().list_contracts():
        executor = RegistryToolExecutor(
            None,  # type: ignore[arg-type]
            TOOL_TEST_SETTINGS,
            catalog=ToolCatalog([BoundTool(contract, never_called_handler)]),
        )
        args = {"unexpected_field": "must fail"}
        if "query" in contract.public_input_model.model_json_schema().get("properties", {}):
            args["query"] = "test"
        result = await executor.execute(contract, args, {"user_id": 1})
        assert not result.ok
        assert result.error is not None
        assert result.error.code == "invalid_input"
        assert result.error.retryable is True
        assert result.error.recommended_action == "replan_arguments"


@pytest.mark.asyncio
async def test_registry_tool_executor_injects_runtime_and_rewrites_tool_name() -> None:
    captured_input = None
    contract = DefaultToolContractProvider().get_contract("order_lookup")
    assert contract is not None

    async def handler(request):
        nonlocal captured_input
        captured_input = request.model_dump(mode="json", exclude_none=True)
        from app.tools.schemas import OrderLookupOutput

        return OrderLookupOutput(result_type="not_found")

    executor = RegistryToolExecutor(
        None,  # type: ignore[arg-type]
        TOOL_TEST_SETTINGS,
        catalog=ToolCatalog([BoundTool(contract, handler)]),
    )

    result = await executor.execute(contract, {"order_id": 42}, {"user_id": 7})

    assert result.ok
    assert result.tool_name == "order_lookup"
    assert captured_input == {"user_id": 7, "order_id": 42, "limit": 5}


@pytest.mark.asyncio
async def test_registry_tool_executor_rejects_llm_supplied_runtime_field() -> None:
    async def never_called_handler(request):  # pragma: no cover
        raise AssertionError("handler should not be called")

    contract = DefaultToolContractProvider().get_contract("order_lookup")
    assert contract is not None
    executor = RegistryToolExecutor(
        None,  # type: ignore[arg-type]
        TOOL_TEST_SETTINGS,
        catalog=ToolCatalog([BoundTool(contract, never_called_handler)]),
    )

    result = await executor.execute(
        contract,
        {"order_id": 42, "user_id": 999},
        {"user_id": 7},
    )

    assert not result.ok
    assert result.tool_name == "order_lookup"
    assert result.error is not None
    assert result.error.code == "invalid_input"


@pytest.mark.asyncio
async def test_registry_tool_executor_returns_timeout_error() -> None:
    contract = DefaultToolContractProvider().get_contract("catalog_search")
    assert contract is not None
    contract.timeout_seconds = 0.001

    async def slow_handler(request):
        await asyncio.sleep(0.05)
        raise AssertionError("timeout should cancel before completion")

    executor = RegistryToolExecutor(
        None,  # type: ignore[arg-type]
        TOOL_TEST_SETTINGS,
        catalog=ToolCatalog([BoundTool(contract, slow_handler)]),
    )

    result = await executor.execute(contract, {"query": "wireless mouse"}, {"user_id": 7})

    assert not result.ok
    assert result.tool_name == "catalog_search"
    assert result.error is not None
    assert result.error.code == "timeout"
    assert result.error.retryable is True
    assert result.error.recommended_action == "retry_once"


@pytest.mark.asyncio
async def test_registry_tool_executor_returns_execution_error_for_unknown_exception() -> None:
    contract = DefaultToolContractProvider().get_contract("catalog_search")
    assert contract is not None

    async def broken_handler(request):
        raise RuntimeError("postgresql://secret@localhost/internal")

    executor = RegistryToolExecutor(
        None,  # type: ignore[arg-type]
        TOOL_TEST_SETTINGS,
        catalog=ToolCatalog([BoundTool(contract, broken_handler)]),
    )

    result = await executor.execute(contract, {"query": "wireless mouse"}, {"user_id": 7})

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "execution_error"
    assert result.error.message == "tool execution failed"
    assert result.error.retryable is False
    assert result.error.recommended_action == "stop"


@pytest.mark.asyncio
async def test_registry_tool_executor_returns_dependency_unavailable() -> None:
    contract = DefaultToolContractProvider().get_contract("catalog_search")
    assert contract is not None

    async def unavailable_handler(request):
        raise SQLAlchemyError("postgresql://secret@localhost/internal")

    executor = RegistryToolExecutor(
        None,  # type: ignore[arg-type]
        TOOL_TEST_SETTINGS,
        catalog=ToolCatalog([BoundTool(contract, unavailable_handler)]),
    )

    result = await executor.execute(contract, {"query": "wireless mouse"}, {"user_id": 7})

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "dependency_unavailable"
    assert result.error.message == "tool dependency is temporarily unavailable"
    assert result.error.retryable is True
    assert result.error.recommended_action == "explain_temporary_unavailability"


def test_stable_tool_error_codes_have_recovery_semantics() -> None:
    from app.tools.contracts import _error_result

    expected = {
        "unknown_tool": (False, "stop"),
        "invalid_input": (True, "replan_arguments"),
        "unauthorized": (False, "request_authentication"),
        "forbidden": (False, "stop"),
        "timeout": (True, "retry_once"),
        "dependency_unavailable": (True, "explain_temporary_unavailability"),
        "execution_error": (False, "stop"),
    }

    for code, (retryable, recommended_action) in expected.items():
        result = _error_result("catalog_search", code)
        assert result.error is not None
        assert result.error.code == code
        assert result.error.retryable is retryable
        assert result.error.recommended_action == recommended_action


@pytest.mark.asyncio
async def test_catalog_search_returns_ranked_wireless_mouse_top_results(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await build_tool_registry(session, settings=TOOL_TEST_SETTINGS).execute(
            "catalog.search",
            {"query": "Codex wireless mouse", "limit": 3},
        )

    assert result.ok
    assert result.output is not None
    assert result.output["result_type"] == "products"
    assert result.output["ranking_strategy"] == "match_score_sales_stock_price"
    assert result.output["products"][0]["title"] == "Razer Codex Viper V3 Pro White"
    assert "Wireless" in result.output["products"][0]["specs"]["connection_type"]


@pytest.mark.asyncio
async def test_catalog_search_uses_query_plan_and_guard(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await CatalogToolService(session, planner=FakeCatalogPlanner()).search(
            CatalogSearchInput(query="Find a wireless Razer mouse", limit=3)
        )

    assert result.result_type == "products"
    assert result.query_plan["planner"] == "fake"
    assert result.query_plan["brands"] == ["Razer"]
    assert result.products[0].brand == "Razer"


@pytest.mark.asyncio
async def test_llm_catalog_planner_parses_guarded_json() -> None:
    chat = FakeChatModel(
        """
        {
          "category": "mouse",
          "brands": ["Logitech"],
          "max_price": 300,
          "filters": {"wireless": "wireless"},
          "keywords": ["fps"],
          "sort": "recommend",
          "supported": true,
          "unsupported_reason": null
        }
        """
    )
    planner = LLMCatalogQueryPlanner(chat)

    plan = await planner.plan_search(CatalogSearchInput(query="FPS mouse under 300", limit=3))

    assert plan.planner == "llm"
    assert plan.query == "FPS mouse under 300"
    assert plan.category == "mouse"
    assert plan.brands == ["Logitech"]
    assert plan.max_price == 300
    assert plan.filters == {"wireless": "wireless"}
    assert chat.calls


@pytest.mark.asyncio
async def test_llm_catalog_planner_applies_explicit_overrides() -> None:
    chat = FakeChatModel(
        '{"category":"keyboard","brands":["Razer"],"filters":{},'
        '"keywords":[],"sort":"recommend","supported":true,"unsupported_reason":null}'
    )
    planner = LLMCatalogQueryPlanner(chat)

    plan = await planner.plan_search(
        CatalogSearchInput(
            query="wireless gear",
            category="mouse",
            brand="Logitech",
            filters={"connection_type": "wireless"},
            limit=3,
        )
    )

    assert plan.category == "mouse"
    assert plan.brands == ["Logitech"]
    assert plan.filters == {"connection_type": "wireless"}


@pytest.mark.asyncio
async def test_rule_catalog_planner_fills_missing_fields_from_typed_preferences() -> None:
    from app.tools.catalog import RuleBasedCatalogQueryPlanner

    request = CatalogSearchInput(
        query="推荐鼠标",
        category="mouse",
        preference_defaults={
            "brands": ["Logitech"],
            "max_price": 500,
            "connection_type": "Wireless",
            "usage": "gaming",
        },
    )

    plan = await RuleBasedCatalogQueryPlanner().plan_search(request)

    assert plan.brands == ["Logitech"]
    assert plan.max_price == 500
    assert plan.filters["connection_type"] == "Wireless"
    assert plan.usage_scenario == "gaming"


@pytest.mark.asyncio
async def test_llm_catalog_planner_keeps_current_conditions_over_preferences() -> None:
    chat = FakeChatModel(
        '{"category":"mouse","brands":["Razer"],"max_price":800,'
        '"filters":{"connection_type":"Wired"},"keywords":[],'
        '"usage_scenario":"office","sort":"recommend","supported":true,'
        '"unsupported_reason":null}'
    )
    planner = LLMCatalogQueryPlanner(chat)

    plan = await planner.plan_search(
        CatalogSearchInput(
            query="这次要 800 元以内的雷蛇有线办公鼠标",
            preference_defaults={
                "brands": ["Logitech"],
                "max_price": 500,
                "connection_type": "Wireless",
                "usage": "gaming",
            },
        )
    )

    assert plan.brands == ["Razer"]
    assert plan.max_price == 800
    assert plan.filters["connection_type"] == "Wired"
    assert plan.usage_scenario == "office"


@pytest.mark.asyncio
async def test_rule_planner_does_not_reparse_explicit_usage_exclusion_as_positive() -> None:
    from app.tools.catalog import RuleBasedCatalogQueryPlanner

    plan = await RuleBasedCatalogQueryPlanner().plan_search(
        CatalogSearchInput(
            query="不要游戏鼠标",
            excluded_usage=["gaming"],
        )
    )

    assert plan.usage_scenario is None
    assert plan.excluded_usage == ["gaming"]


@pytest.mark.asyncio
async def test_llm_planner_explicit_brand_exclusion_overrides_positive_model_output() -> None:
    chat = FakeChatModel(
        '{"category":"mouse","brands":["Logitech"],"filters":{},'
        '"keywords":[],"sort":"recommend","supported":true,"unsupported_reason":null}'
    )
    planner = LLMCatalogQueryPlanner(chat)

    plan = await planner.plan_search(
        CatalogSearchInput(
            query="不要 Logitech 鼠标",
            excluded_brands=["Logitech"],
        )
    )

    assert plan.brands == []
    assert plan.excluded_brands == ["Logitech"]


def test_build_catalog_planner_is_opt_in() -> None:
    planner = build_catalog_planner(
        SimpleNamespace(catalog_llm_planner_enabled=False)  # type: ignore[arg-type]
    )

    assert planner is None


def test_build_catalog_planner_uses_llm_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = FakeChatModel('{"category":"mouse"}')
    monkeypatch.setattr("app.tools.contracts.build_chat_model", lambda settings: chat)

    planner = build_catalog_planner(
        SimpleNamespace(catalog_llm_planner_enabled=True)  # type: ignore[arg-type]
    )

    assert isinstance(planner, LLMCatalogQueryPlanner)
    assert planner.chat_model is chat


def test_build_catalog_planner_skips_when_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.tools.contracts.build_chat_model", lambda settings: None)

    planner = build_catalog_planner(
        SimpleNamespace(catalog_llm_planner_enabled=True)  # type: ignore[arg-type]
    )

    assert planner is None


@pytest.mark.asyncio
async def test_catalog_search_falls_back_for_category_invalid_llm_filter(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    chat = FakeChatModel(
        '{"category":"mouse","brands":[],"filters":{"type":"FPS"},'
        '"keywords":["wireless","FPS","mouse"],"sort":"recommend",'
        '"supported":true,"unsupported_reason":null}'
    )
    async with db_session_factory() as session:
        result = await CatalogToolService(
            session,
            planner=LLMCatalogQueryPlanner(chat),
        ).search(CatalogSearchInput(query="Recommend a wireless FPS mouse under 300", limit=3))

    assert result.result_type == "products"
    assert result.query_plan["planner"] == "rule_based_fallback"
    assert "unsupported filters for mouse" in result.query_plan["fallback_reason"]


@pytest.mark.asyncio
async def test_catalog_search_normalizes_localized_filter_key_and_value(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    chat = FakeChatModel(
        '{"category":"mouse","brands":[],"filters":{"\u8fde\u63a5\u65b9\u5f0f":"\u65e0\u7ebf"},'
        '"keywords":["mouse"],"sort":"recommend",'
        '"supported":true,"unsupported_reason":null}'
    )
    async with db_session_factory() as session:
        result = await CatalogToolService(
            session,
            planner=LLMCatalogQueryPlanner(chat),
        ).search(CatalogSearchInput(query="Recommend a wireless mouse", limit=3))

    assert result.result_type == "products"
    assert result.query_plan["planner"] == "llm"
    assert result.query_plan["filters"] == {"connection_type": "Wireless"}


@pytest.mark.asyncio
async def test_catalog_search_falls_back_when_planner_fails(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await CatalogToolService(session, planner=BrokenCatalogPlanner()).search(
            CatalogSearchInput(query="Codex wireless mouse", limit=3)
        )

    assert result.result_type == "products"
    assert result.query_plan["planner"] == "rule_based_fallback"
    assert "planner unavailable" in result.query_plan["fallback_reason"]


@pytest.mark.asyncio
async def test_catalog_search_returns_unsupported_query(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await CatalogToolService(session).search(
            CatalogSearchInput(query="Which mouse has the fastest month over month growth?")
        )

    assert result.result_type == "empty"
    assert result.ranking_strategy == "unsupported_query"
    assert result.query_plan["supported"] is False


@pytest.mark.asyncio
async def test_catalog_facets_returns_mouse_brands(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await build_tool_registry(session, settings=TOOL_TEST_SETTINGS).execute(
            "catalog.facets",
            {"query": "what mouse brands do you sell", "facet": "brand", "category": "mouse"},
        )

    assert result.ok
    assert result.output is not None
    assert result.output["result_type"] == "facets"
    assert result.output["facet"] == "brand"
    values = {item["value"] for item in result.output["items"]}
    assert {"Logitech", "Razer"} <= values


@pytest.mark.asyncio
async def test_catalog_facets_infers_brand_facet_from_query_only(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await build_tool_registry(session, settings=TOOL_TEST_SETTINGS).execute(
            "catalog.facets",
            {"query": "what mouse brands do you sell"},
        )

    assert result.ok
    assert result.output is not None
    assert result.output["result_type"] == "facets"
    assert result.output["facet"] == "brand"
    assert result.output["category"] == "mouse"
    values = {item["value"] for item in result.output["items"]}
    assert {"Logitech", "Razer"} <= values


@pytest.mark.asyncio
async def test_catalog_facets_infers_category_facet_from_query_only(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await build_tool_registry(session, settings=TOOL_TEST_SETTINGS).execute(
            "catalog.facets",
            {"query": "what peripheral categories does Razer sell"},
        )

    assert result.ok
    assert result.output is not None
    assert result.output["result_type"] == "facets"
    assert result.output["facet"] == "category"
    assert result.output["brand"] == "Razer"
    assert result.output["items"]


@pytest.mark.asyncio
async def test_catalog_facets_returns_spec_values(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await build_tool_registry(session, settings=TOOL_TEST_SETTINGS).execute(
            "catalog.facets",
            {
                "query": "what keyboard switches are available",
                "facet": "spec_value",
                "category": "keyboard",
                "spec_key": "switches",
            },
        )

    assert result.ok
    assert result.output is not None
    assert result.output["result_type"] == "facets"
    assert result.output["facet"] == "spec_value"
    assert any("Red" in item["value"] for item in result.output["items"])


@pytest.mark.asyncio
async def test_catalog_compare_resolves_natural_language_candidates(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await build_tool_registry(session, settings=TOOL_TEST_SETTINGS).execute(
            "catalog.compare",
            {"query": "Compare Codex G502 and Viper for FPS", "limit": 5},
        )

    assert result.ok
    assert result.output is not None
    titles = [product["title"] for product in result.output["products"]]
    assert "Logitech Codex G502 Hero Black" in titles
    assert "Razer Codex Viper V3 Pro White" in titles
    assert result.output["comparison_fields"]


@pytest.mark.asyncio
async def test_catalog_compare_uses_compare_plan_fields(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await CatalogToolService(session, planner=FakeCatalogPlanner()).compare(
            CatalogCompareInput(query="Compare G502 and Viper for FPS", limit=5)
        )

    assert result.result_type == "comparison"
    assert result.query_plan["compare_plan"]["planner"] == "fake"
    assert result.comparison_fields == [
        "price",
        "stock",
        "max_dpi",
        "weight_g",
        "connection_type",
    ]
    assert result.products
    brands = {product.brand for product in result.products}
    assert {"Logitech", "Razer"} <= brands
    assert sum(1 for product in result.products if product.brand == "Logitech") >= 1
    assert sum(1 for product in result.products if product.brand == "Razer") >= 1


@pytest.mark.asyncio
async def test_direct_sku_compare_omits_inactive_sku_and_spu(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        rows = (
            await session.execute(
                select(Sku.id, Spu.id)
                .join(Spu, Sku.spu_id == Spu.id)
                .where(Sku.title.in_([
                    "Logitech Codex G502 Hero Black",
                    "Razer Codex Viper V3 Pro White",
                ]))
                .order_by(Sku.id)
            )
        ).all()
        assert len(rows) == 2
        inactive_sku_id, _ = rows[0]
        active_sku_id, inactive_spu_id = rows[1]
        await session.execute(update(Sku).where(Sku.id == inactive_sku_id).values(status=0))
        await session.execute(update(Spu).where(Spu.id == inactive_spu_id).values(status=0))
        await session.flush()

        result = await CatalogToolService(session).compare(
            CatalogCompareInput(
                query="compare selected products",
                sku_ids=[inactive_sku_id, active_sku_id],
                limit=5,
            )
        )

    assert result.products == []


def test_direct_sku_compare_statement_requires_active_sku_and_spu() -> None:
    statement = catalog_tools._active_sku_rows_statement([11, 22])
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "sku.status" in sql
    assert "spu.status" in sql


@pytest.mark.asyncio
async def test_llm_catalog_compare_planner_parses_guarded_json() -> None:
    chat = FakeChatModel(
        """
        {
          "category": "mouse",
          "items": ["G502", "Viper"],
          "brands": ["Logitech", "Razer"],
          "comparison_fields": ["price", "stock", "max_dpi", "weight_g"],
          "scenario": "FPS",
          "supported": true,
          "unsupported_reason": null
        }
        """
    )
    planner = LLMCatalogQueryPlanner(chat)

    plan = await planner.plan_compare(
        CatalogCompareInput(query="Compare G502 and Viper for FPS", limit=5)
    )

    assert plan.planner == "llm"
    assert plan.items == ["G502", "Viper"]
    assert plan.brands == ["Logitech", "Razer"]
    assert plan.comparison_fields == ["price", "stock", "max_dpi", "weight_g"]


@pytest.mark.asyncio
async def test_order_lookup_returns_candidates_or_single_order_with_user_isolation(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        registry = build_tool_registry(session, settings=TOOL_TEST_SETTINGS)
        candidates = await registry.execute("order.lookup", {"user_id": 1, "limit": 5})
        single = await registry.execute(
            "order.lookup",
            {"user_id": 1, "order_id": 202607020001},
        )
        isolated = await registry.execute(
            "order.lookup",
            {"user_id": 999, "order_id": 202607020001},
        )

    assert candidates.ok
    assert candidates.output is not None
    assert candidates.output["result_type"] == "order_candidates"
    assert candidates.output["candidates"][0]["id"] == 202607020001

    assert single.ok
    assert single.output is not None
    assert single.output["result_type"] == "single_order"
    assert single.output["order"]["id"] == 202607020001

    assert isolated.ok
    assert isolated.output is not None
    assert isolated.output["result_type"] == "not_found"


@pytest.mark.asyncio
async def test_order_lookup_extracts_order_id_from_query(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    contract = DefaultToolContractProvider().get_contract("order_lookup")
    assert contract is not None

    async with db_session_factory() as session:
        executor = RegistryToolExecutor(session, TOOL_TEST_SETTINGS)
        result = await executor.execute(
            contract,
            {"query": "please check order 202607020001"},
            {"user_id": 1},
        )

    assert result.ok
    assert result.output is not None
    assert result.output["result_type"] == "single_order"
    assert result.output["order"]["id"] == 202607020001


@pytest.mark.asyncio
async def test_order_lookup_query_without_order_id_returns_candidates(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    contract = DefaultToolContractProvider().get_contract("order_lookup")
    assert contract is not None

    async with db_session_factory() as session:
        executor = RegistryToolExecutor(session, TOOL_TEST_SETTINGS)
        result = await executor.execute(
            contract,
            {"query": "show my recent orders"},
            {"user_id": 1},
        )

    assert result.ok
    assert result.output is not None
    assert result.output["result_type"] == "order_candidates"


@pytest.mark.asyncio
async def test_policy_and_knowledge_search_support_hybrid_retrieval() -> None:
    service = _knowledge_service()
    policy = await service.search_policy(DocumentSearchInput(query="return refund", limit=3))
    knowledge = await service.search_knowledge(
        DocumentSearchInput(query="Logitech Razer brand", limit=3)
    )

    assert policy.result_type == "documents"
    assert policy.search_strategy == "hybrid"
    assert policy.documents[0].document_type in {"policy", "store_rule", "faq"}
    assert "retrieval_debug" in policy.documents[0].metadata

    assert knowledge.result_type == "documents"
    assert knowledge.search_strategy == "hybrid"
    assert knowledge.documents[0].document_type in {
        "brand",
        "peripheral_knowledge",
        "faq",
        "store_rule",
    }
    assert any(document.document_type == "brand" for document in knowledge.documents)


@pytest.mark.asyncio
async def test_document_search_can_select_retrieval_mode() -> None:
    service = _knowledge_service()
    bm25 = await service.search_knowledge(
        DocumentSearchInput(query="keyboard magnetic switch", retrieval_mode="bm25", limit=3)
    )
    vector = await service.search_knowledge(
        DocumentSearchInput(query="Wooting", retrieval_mode="vector", limit=3)
    )

    assert bm25.search_strategy == "bm25"
    assert bm25.result_type == "documents"
    assert bm25.documents[0].score > 0

    assert vector.search_strategy == "vector"
    assert vector.result_type == "documents"
    assert vector.documents[0].metadata["retrieval_debug"]["vector_score"] > 0


@pytest.mark.asyncio
async def test_tool_registry_returns_stable_errors(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        registry = build_tool_registry(session, settings=TOOL_TEST_SETTINGS)
        unknown = await registry.execute("missing.tool", {})
        invalid = await registry.execute("catalog.search", {"limit": 3})

    assert not unknown.ok
    assert unknown.error is not None
    assert unknown.error.code == "unknown_tool"

    assert not invalid.ok
    assert invalid.error is not None
    assert invalid.error.code == "invalid_input"


def test_catalog_sql_guard_rejects_unsafe_sql() -> None:
    validate_catalog_sql("SELECT sku.id FROM sku LIMIT 5")

    with pytest.raises(ValueError):
        validate_catalog_sql("SELECT * FROM order_info LIMIT 5")

    with pytest.raises(ValueError):
        validate_catalog_sql("DELETE FROM sku")

    with pytest.raises(ValueError):
        validate_catalog_sql("SELECT sku.id FROM sku")


def test_product_query_guard_rejects_unsupported_fields() -> None:
    with pytest.raises(ValueError):
        validate_product_query_plan(
            ProductQueryPlan(
                query="find products",
                filters={"credit_card": "anything"},
            )
        )

    with pytest.raises(ValueError):
        validate_product_query_plan(
            ProductQueryPlan(
                query="find products",
                category="order",
            )
        )
