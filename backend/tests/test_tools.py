from collections.abc import Callable
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.tools.catalog import (
    CatalogComparePlan,
    CatalogToolService,
    LLMCatalogQueryPlanner,
    ProductQueryPlan,
    validate_catalog_sql,
    validate_product_query_plan,
)
from app.tools.knowledge import (
    KnowledgeRetrievalToolService,
    KnowledgeVectorIndex,
    KnowledgeVectorIndexChunk,
)
from app.tools.registry import build_catalog_planner, build_tool_registry
from app.tools.schemas import CatalogCompareInput, CatalogSearchInput, DocumentSearchInput

TOOL_TEST_SETTINGS = Settings(llm_api_key="", catalog_llm_planner_enabled=False)


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
        "catalog.search",
        "knowledge.search",
        "order.lookup",
        "policy.search",
    ]


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


def test_build_catalog_planner_is_opt_in() -> None:
    planner = build_catalog_planner(
        SimpleNamespace(catalog_llm_planner_enabled=False)  # type: ignore[arg-type]
    )

    assert planner is None


def test_build_catalog_planner_uses_llm_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = FakeChatModel('{"category":"mouse"}')
    monkeypatch.setattr("app.tools.registry.build_chat_model", lambda settings: chat)

    planner = build_catalog_planner(
        SimpleNamespace(catalog_llm_planner_enabled=True)  # type: ignore[arg-type]
    )

    assert isinstance(planner, LLMCatalogQueryPlanner)
    assert planner.chat_model is chat


def test_build_catalog_planner_skips_when_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.tools.registry.build_chat_model", lambda settings: None)

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
