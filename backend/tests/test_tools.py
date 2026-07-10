from collections.abc import Callable

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.tools.catalog import validate_catalog_sql
from app.tools.knowledge import KnowledgeKeywordToolService
from app.tools.registry import build_tool_registry
from app.tools.schemas import DocumentSearchInput


@pytest.mark.asyncio
async def test_tool_registry_exposes_expected_business_tools(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        registry = build_tool_registry(session)

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
        result = await build_tool_registry(session).execute(
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
async def test_catalog_compare_resolves_natural_language_candidates(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await build_tool_registry(session).execute(
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
async def test_order_lookup_returns_candidates_or_single_order_with_user_isolation(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        registry = build_tool_registry(session)
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
    service = KnowledgeKeywordToolService()
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
    service = KnowledgeKeywordToolService()
    bm25 = await service.search_knowledge(
        DocumentSearchInput(query="keyboard magnetic switch", retrieval_mode="bm25", limit=3)
    )
    keyword = await service.search_knowledge(
        DocumentSearchInput(query="Wooting", retrieval_mode="keyword", limit=3)
    )

    assert bm25.search_strategy == "bm25"
    assert bm25.result_type == "documents"
    assert bm25.documents[0].score > 0

    assert keyword.search_strategy == "keyword"
    assert keyword.result_type == "documents"
    assert keyword.documents[0].metadata["retrieval_debug"]["keyword_score"] > 0


@pytest.mark.asyncio
async def test_tool_registry_returns_stable_errors(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        registry = build_tool_registry(session)
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
