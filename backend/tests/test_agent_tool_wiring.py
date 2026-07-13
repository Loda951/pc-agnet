from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import AgentRuntime, _catalog_search_input
from app.core.config import Settings
from app.schemas.catalog import ProductSearchRequest
from app.schemas.chat import ChatRequest
from app.schemas.context import (
    MemoryChanges,
    PreparedTurn,
    StructuredMemory,
    WorkingMemoryV2,
)
from app.tools.schemas import ToolError, ToolExecutionResult

PRODUCT = {
    "spu_id": 10,
    "sku_id": 101,
    "title": "Razer Test Mouse",
    "brand": "Razer",
    "category": "mouse",
    "price": "399.00",
    "stock": 8,
    "sales_count": 12,
    "specs": {"connection_type": "Wireless"},
    "image_url": None,
}
SECOND_PRODUCT = {
    **PRODUCT,
    "spu_id": 11,
    "sku_id": 102,
    "title": "Logitech Fresh Mouse",
    "brand": "Logitech",
    "price": "299.00",
}
ORDER = {
    "id": 202607020001,
    "status": 3,
    "status_label": "已发货",
    "pay_amount": "399.00",
    "created_at": "2026-07-02T10:00:00",
    "items": [
        {
            "id": 1,
            "sku_id": 101,
            "sku_name": "Razer Test Mouse",
            "sku_specs": {"connection_type": "Wireless"},
            "price": "399.00",
            "quantity": 1,
        }
    ],
    "logistics": {
        "express_company": "顺丰",
        "logistic_no": "SF123",
        "status": 2,
        "trace": [],
    },
}


class FakeContextService:
    def __init__(self, prepared: PreparedTurn):
        self.prepared = prepared
        self.prepare_calls: list[tuple[int, int | None, str]] = []
        self.completed_outcomes: list[dict[str, Any]] = []

    async def prepare_turn(
        self, user_id: int, conversation_id: int | None, message: str
    ) -> PreparedTurn:
        self.prepare_calls.append((user_id, conversation_id, message))
        return self.prepared

    async def complete_turn(
        self, prepared_turn: PreparedTurn, outcome: dict[str, Any]
    ) -> MemoryChanges:
        assert prepared_turn is self.prepared
        self.completed_outcomes.append(outcome)
        return MemoryChanges(
            working_memory=prepared_turn.working_memory,
            audit={"retained_turns": prepared_turn.retained_turns},
        )


class FakeToolRegistry:
    def __init__(self, results: dict[str, ToolExecutionResult]):
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, name: str, input_data: dict[str, Any]) -> ToolExecutionResult:
        self.calls.append((name, input_data))
        return self.results[name]


class EmptyKnowledgeService:
    async def retrieve(self, query: str) -> list:
        return []


@pytest.mark.asyncio
async def test_sync_runtime_uses_context_and_catalog_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=51,
        run_id=61,
        message="推荐 500 元以内无线鼠标",
        retained_turns=2,
    )
    context = FakeContextService(prepared)
    registry = FakeToolRegistry(
        {
            "catalog.search": ToolExecutionResult(
                tool_name="catalog.search",
                ok=True,
                output={
                    "result_type": "products",
                    "products": [PRODUCT],
                    "ranking_strategy": "test",
                    "query_plan": {
                        "query": "无线鼠标",
                        "category": "mouse",
                        "max_price": "500",
                        "filters": {"connection_type": "Wireless"},
                    },
                },
            )
        }
    )

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        knowledge_service=EmptyKnowledgeService(),
        context_service=context,
        tool_registry=registry,
    )

    response = await runtime.run(ChatRequest(message=prepared.message), user_id=7)

    assert context.prepare_calls == [(7, None, prepared.message)]
    assert [name for name, _ in registry.calls] == ["catalog.search"]
    assert response.products[0].sku_id == 101
    assert context.completed_outcomes
    assert "working_memory" not in context.completed_outcomes[0]


@pytest.mark.asyncio
async def test_compare_followup_resolves_ordinals_to_working_memory_sku_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=52,
        run_id=62,
        message="对比第一个和第二个",
        working_memory=WorkingMemoryV2.model_validate(
            {
                "catalog": {
                    "query_plan": {"query": "mouse", "category": "mouse"},
                    "candidate_spu_ids": [10, 11],
                    "candidate_sku_ids": [101, 102],
                }
            }
        ),
    )
    context = FakeContextService(prepared)
    registry = FakeToolRegistry(
        {
            "catalog.compare": ToolExecutionResult(
                tool_name="catalog.compare",
                ok=True,
                output={
                    "result_type": "comparison",
                    "products": [PRODUCT, SECOND_PRODUCT],
                    "comparison_fields": ["price", "connection_type"],
                    "missing_fields": {},
                    "query_plan": {
                        "mode": "direct_sku_ids",
                        "sku_ids": [101, 102],
                        "query": prepared.message,
                        "compare_plan": None,
                    },
                },
            )
        }
    )

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        knowledge_service=EmptyKnowledgeService(),
        context_service=context,
        tool_registry=registry,
    )

    response = await runtime.run(ChatRequest(message=prepared.message), user_id=7)

    assert registry.calls == [
        (
            "catalog.compare",
            {"query": prepared.message, "sku_ids": [101, 102], "limit": 5},
        )
    ]
    assert [product.sku_id for product in response.products] == [101, 102]
    assert response.products[1].title == "Logitech Fresh Mouse"


@pytest.mark.asyncio
async def test_current_catalog_conditions_beat_working_and_long_term_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=53,
        run_id=63,
        message="这次要 800 元以内 Logitech 无线办公鼠标",
        working_memory=WorkingMemoryV2.model_validate(
            {
                "catalog": {
                    "query_plan": {
                        "query": "gaming mouse",
                        "category": "mouse",
                        "brands": ["Razer"],
                        "max_price": 500,
                        "filters": {"connection_type": "Wired"},
                        "usage_scenario": "gaming",
                    }
                }
            }
        ),
        memory=[
            StructuredMemory(
                id=71,
                scope="user",
                fact_type="preference",
                key="brand_preference",
                value="偏好 SteelSeries 品牌",
                value_json={"brand": "SteelSeries", "negated": False},
                confidence=0.8,
            ),
            StructuredMemory(
                id=72,
                scope="user",
                fact_type="preference",
                key="budget_preference",
                value="偏好 300 元以内预算",
                value_json={"amount": 300, "maximum": True},
                confidence=0.8,
            ),
        ],
    )
    context = FakeContextService(prepared)
    registry = FakeToolRegistry(
        {
            "catalog.search": ToolExecutionResult(
                tool_name="catalog.search",
                ok=True,
                output={
                    "result_type": "empty",
                    "products": [],
                    "ranking_strategy": "test",
                    "query_plan": {},
                },
            )
        }
    )

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        knowledge_service=EmptyKnowledgeService(),
        context_service=context,
        tool_registry=registry,
    )

    await runtime.run(ChatRequest(message=prepared.message), user_id=7)

    _, tool_input = registry.calls[0]
    assert tool_input["brands"] == ["Logitech"]
    assert tool_input["max_price"] == "800"
    assert tool_input["filters"]["connection_type"] == "Wireless"
    assert tool_input["usage"] == "office"
    assert tool_input["preference_defaults"] == {
        "brands": ["Razer"],
        "excluded_brands": [],
        "excluded_usage": [],
        "max_price": "500",
        "connection_type": "Wired",
        "usage": "gaming",
    }
    assert context.completed_outcomes[0]["applied_memory_ids"] == []


@pytest.mark.asyncio
async def test_sync_runtime_uses_authenticated_order_lookup_and_preserves_order_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=54,
        run_id=64,
        message="帮我查最近订单",
    )
    context = FakeContextService(prepared)

    class SequenceRegistry:
        def __init__(self):
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def execute(
            self, name: str, input_data: dict[str, Any]
        ) -> ToolExecutionResult:
            self.calls.append((name, input_data))
            if len(self.calls) == 1:
                return ToolExecutionResult(
                    tool_name=name,
                    ok=True,
                    output={
                        "result_type": "order_candidates",
                        "order": None,
                        "candidates": [
                            {
                                "id": ORDER["id"],
                                "status": ORDER["status"],
                                "status_label": ORDER["status_label"],
                                "pay_amount": ORDER["pay_amount"],
                                "created_at": ORDER["created_at"],
                                "item_count": 1,
                                "first_item_name": "Razer Test Mouse",
                                "logistic_no": "SF123",
                            }
                        ],
                    },
                )
            return ToolExecutionResult(
                tool_name=name,
                ok=True,
                output={"result_type": "single_order", "order": ORDER, "candidates": []},
            )

    registry = SequenceRegistry()

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        knowledge_service=EmptyKnowledgeService(),
        context_service=context,
        tool_registry=cast(Any, registry),
    )

    response = await runtime.run(ChatRequest(message=prepared.message), user_id=7)

    assert registry.calls == [
        ("order.lookup", {"user_id": 7, "order_id": None, "limit": 1}),
        ("order.lookup", {"user_id": 7, "order_id": ORDER["id"], "limit": 1}),
    ]
    assert response.order is not None
    assert response.order.id == ORDER["id"]
    assert response.order.logistics is not None
    assert response.order.logistics.logistic_no == "SF123"


@pytest.mark.asyncio
async def test_stream_registry_failure_emits_tool_error_and_finishes_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=55,
        run_id=65,
        message="推荐无线鼠标",
        working_memory=WorkingMemoryV2.model_validate(
            {
                "catalog": {
                    "query_plan": {"query": "old mouse", "category": "mouse"},
                    "candidate_spu_ids": [99],
                    "candidate_sku_ids": [999],
                }
            }
        ),
    )
    context = FakeContextService(prepared)
    registry = FakeToolRegistry(
        {
            "catalog.search": ToolExecutionResult(
                tool_name="catalog.search",
                ok=False,
                error=ToolError(code="catalog_unavailable", message="temporary outage"),
            )
        }
    )

    class FakeSession:
        async def commit(self) -> None:
            pass

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

        async def fail_run(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, FakeSession()),
        Settings(llm_api_key=""),
        knowledge_service=EmptyKnowledgeService(),
        context_service=context,
        tool_registry=registry,
    )

    events = [
        event
        async for event in runtime.run_stream(
            ChatRequest(message=prepared.message), user_id=7
        )
    ]

    catalog_events = [
        event
        for event in events
        if event["type"] == "tool_call" and event.get("tool_name") == "catalog.search"
    ]
    assert [event["status"] for event in catalog_events] == ["started", "error"]
    assert catalog_events[-1]["output"]["error"]["code"] == "catalog_unavailable"
    assert not any(event["type"] == "error" for event in events)
    assert events[-1]["type"] == "done"
    assert events[-1]["response"]["products"] == []
    assert context.completed_outcomes
    assert "product_search" not in context.completed_outcomes[0]["parsed"]
    assert "working_memory" not in context.completed_outcomes[0]


@pytest.mark.asyncio
async def test_v2_working_query_plan_routes_generic_catalog_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=56,
        run_id=66,
        message="换成无线",
        working_memory=WorkingMemoryV2.model_validate(
            {
                "catalog": {
                    "query_plan": {
                        "query": "mouse",
                        "category": "mouse",
                        "max_price": 500,
                        "filters": {"connection_type": "Wired"},
                    },
                    "candidate_sku_ids": [101],
                }
            }
        ),
    )
    context = FakeContextService(prepared)
    registry = FakeToolRegistry(
        {
            "catalog.search": ToolExecutionResult(
                tool_name="catalog.search",
                ok=True,
                output={
                    "result_type": "empty",
                    "products": [],
                    "ranking_strategy": "test",
                    "query_plan": {},
                },
            )
        }
    )

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        knowledge_service=EmptyKnowledgeService(),
        context_service=context,
        tool_registry=registry,
    )

    response = await runtime.run(ChatRequest(message=prepared.message), user_id=7)

    assert response.intent == "product_recommendation"
    assert registry.calls[0][0] == "catalog.search"
    tool_input = registry.calls[0][1]
    assert tool_input["category"] == "mouse"
    assert tool_input["filters"]["connection_type"] == "Wireless"
    assert tool_input["preference_defaults"]["max_price"] == "500"


def test_three_turn_search_compare_followup_reuses_complete_safe_search_plan() -> None:
    working_memory = WorkingMemoryV2.model_validate(
        {
            "catalog": {
                "query_plan": {
                    "query": "fps ergonomic mouse",
                    "category": "mouse",
                    "brands": ["Razer"],
                    "excluded_brands": ["Logitech"],
                    "excluded_usage": ["office"],
                    "min_price": 200,
                    "max_price": 500,
                    "filters": {
                        "connection_type": "Wired",
                        "max_dpi": "20000",
                        "hand_orientation": "Right",
                        "tracking_method": "Optical",
                    },
                    "keywords": ["fps", "lightweight"],
                    "usage_scenario": "gaming",
                    "sort": "price_asc",
                    "limit": 6,
                },
                "comparison": {
                    "query": "对比第一个和第二个",
                    "sku_ids": [101, 102],
                    "comparison_fields": ["price", "max_dpi"],
                },
                "candidate_sku_ids": [101, 102],
            }
        }
    )
    state = cast(
        Any,
        {
            "message": "换成无线",
            "working_memory": working_memory.model_dump(mode="json"),
            "memory": [],
        },
    )

    tool_input, _ = _catalog_search_input(
        state,
        ProductSearchRequest(
            query="换成无线",
            filters={"connection_type": "Wireless"},
            limit=6,
        ),
    )
    payload = tool_input.model_dump(mode="json")

    assert payload["query"] == "fps ergonomic mouse"
    assert payload["category"] == "mouse"
    assert payload["brands"] == ["Razer"]
    assert payload["excluded_brands"] == ["Logitech"]
    assert payload["excluded_usage"] == ["office"]
    assert payload["min_price"] == "200"
    assert payload["max_price"] == "500"
    assert payload["filters"] == {
        "connection_type": "Wireless",
        "max_dpi": "20000",
        "hand_orientation": "Right",
        "tracking_method": "Optical",
    }
    assert payload["keywords"] == ["fps", "lightweight"]
    assert payload["usage"] == "gaming"
    assert payload["sort"] == "price_asc"
    assert "换成无线" not in payload["keywords"]


@pytest.mark.asyncio
async def test_bare_v2_brand_exclusion_routes_back_to_catalog_search() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))
    working_memory = WorkingMemoryV2.model_validate(
        {
            "catalog": {
                "query_plan": {
                    "query": "gaming mouse",
                    "category": "mouse",
                    "brands": ["Razer"],
                }
            }
        }
    )

    state = await runtime._route_intent(
        cast(
            Any,
            {
                "message": "不要 Razer",
                "working_memory": working_memory.model_dump(mode="json"),
                "memory": [],
            },
        )
    )

    assert state["intent"] == "product_recommendation"
    assert "product_search" in state["parsed"]


def test_v2_followup_brand_exclusion_removes_historical_positive_brand() -> None:
    state = cast(
        Any,
        {
            "message": "不要 Razer",
            "working_memory": WorkingMemoryV2.model_validate(
                {
                    "catalog": {
                        "query_plan": {
                            "query": "gaming mouse",
                            "category": "mouse",
                            "brands": ["Razer"],
                            "keywords": ["gaming"],
                            "usage_scenario": "gaming",
                            "limit": 6,
                        }
                    }
                }
            ).model_dump(mode="json"),
            "memory": [],
        },
    )

    tool_input, _ = _catalog_search_input(
        state,
        ProductSearchRequest(query="不要 Razer", limit=6),
    )

    assert tool_input.brands == []
    assert tool_input.excluded_brands == ["Razer"]


def test_v2_category_switch_uses_current_query_and_drops_old_constraints() -> None:
    state = cast(
        Any,
        {
            "message": "换成无线键盘",
            "working_memory": WorkingMemoryV2.model_validate(
                {
                    "catalog": {
                        "query_plan": {
                            "query": "fps ergonomic mouse",
                            "category": "mouse",
                            "max_price": 500,
                            "filters": {
                                "connection_type": "Wired",
                                "max_dpi": "20000",
                                "hand_orientation": "Right",
                            },
                            "keywords": ["fps", "lightweight"],
                            "usage_scenario": "gaming",
                            "sort": "price_asc",
                            "limit": 6,
                        }
                    }
                }
            ).model_dump(mode="json"),
            "memory": [],
        },
    )

    tool_input, _ = _catalog_search_input(
        state,
        ProductSearchRequest(
            query="无线键盘",
            category="keyboard",
            filters={"connection_type": "Wireless"},
            limit=6,
        ),
    )

    assert tool_input.query == "无线键盘"
    assert tool_input.category == "keyboard"
    assert tool_input.filters == {"connection_type": "Wireless"}
    assert tool_input.keywords == []
    assert tool_input.usage is None
    assert tool_input.max_price == 500
    assert tool_input.sort == "price_asc"
    assert tool_input.preference_defaults.brands == []
    assert tool_input.preference_defaults.connection_type is None
    assert tool_input.preference_defaults.usage is None


@pytest.mark.asyncio
async def test_failed_stream_run_uses_compact_context_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=57,
        run_id=67,
        message="推荐鼠标",
        estimated_token_count=42,
        retained_turns=2,
        dropped_turns=3,
    )
    captured: dict[str, Any] = {}

    class FakeSession:
        async def commit(self) -> None:
            captured["committed"] = True

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def fail_run(
            self,
            run_id: int,
            intent: str | None,
            state: dict[str, Any],
            error: dict[str, str],
        ) -> None:
            captured.update(
                {"run_id": run_id, "intent": intent, "state": state, "error": error}
            )

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, FakeSession()),
        Settings(llm_api_key=""),
        knowledge_service=EmptyKnowledgeService(),
        context_service=FakeContextService(prepared),
        tool_registry=FakeToolRegistry({}),
    )
    state = cast(
        Any,
        {
            "run_id": 67,
            "intent": "product_recommendation",
            "prepared_turn": prepared,
            "history": [{"role": "user", "content": "secret history"}],
            "memory": [{"id": 1, "value": "secret memory"}],
            "working_memory": {"catalog": {"candidate_sku_ids": [101]}},
            "applied_memory_ids": [1],
        },
    )

    await runtime._mark_stream_failed(state, "RuntimeError", "boom")

    audit = captured["state"]
    assert "prepared_turn" not in audit
    assert "history" not in audit
    assert "memory" not in audit
    assert "working_memory" not in audit
    assert audit["estimated_token_count"] == 42
    assert audit["retained_turns"] == 2
    assert audit["dropped_turns"] == 3
    assert audit["applied_memory_ids"] == [1]
    assert captured["committed"] is True


@pytest.mark.asyncio
async def test_sync_runtime_failure_finalizes_compact_failed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=58,
        run_id=68,
        message="推荐鼠标",
        estimated_token_count=21,
        retained_turns=1,
        dropped_turns=4,
    )
    captured: dict[str, Any] = {}

    class FakeSession:
        async def commit(self) -> None:
            captured["committed"] = True

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

        async def fail_run(self, run_id, intent, state, error) -> None:
            captured.update(run_id=run_id, intent=intent, state=state, error=error)

    class RaisingRegistry:
        async def execute(self, name: str, input_data: dict[str, Any]):
            raise RuntimeError("registry boom")

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, FakeSession()),
        Settings(llm_api_key=""),
        knowledge_service=EmptyKnowledgeService(),
        context_service=FakeContextService(prepared),
        tool_registry=cast(Any, RaisingRegistry()),
    )

    with pytest.raises(RuntimeError, match="AI 回复生成失败"):
        await runtime.run(ChatRequest(message=prepared.message), user_id=7)

    assert captured["run_id"] == 68
    assert captured["state"]["estimated_token_count"] == 21
    assert captured["state"]["retained_turns"] == 1
    assert captured["state"]["dropped_turns"] == 4
    assert "history" not in captured["state"]
    assert "memory" not in captured["state"]
    assert "working_memory" not in captured["state"]
    assert captured["committed"] is True


@pytest.mark.asyncio
async def test_current_negative_catalog_preferences_are_explicit_exclusions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=59,
        run_id=69,
        message="不要 Logitech 的游戏无线鼠标",
    )
    context = FakeContextService(prepared)
    registry = FakeToolRegistry(
        {
            "catalog.search": ToolExecutionResult(
                tool_name="catalog.search",
                ok=True,
                output={
                    "result_type": "empty",
                    "products": [],
                    "ranking_strategy": "test",
                    "query_plan": {},
                },
            )
        }
    )

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        knowledge_service=EmptyKnowledgeService(),
        context_service=context,
        tool_registry=registry,
    )

    await runtime.run(ChatRequest(message=prepared.message), user_id=7)

    tool_input = registry.calls[0][1]
    assert tool_input["brands"] == []
    assert tool_input["excluded_brands"] == ["Logitech"]
    assert tool_input["excluded_usage"] == ["gaming"]
    assert tool_input["filters"]["connection_type"] == "Wired"


@pytest.mark.asyncio
async def test_negative_long_term_preferences_become_query_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=60,
        run_id=70,
        message="推荐鼠标",
        memory=[
            StructuredMemory(
                id=81,
                scope="user",
                fact_type="preference",
                key="brand_preference",
                value="不偏好 Logitech 品牌",
                value_json={"brand": "Logitech", "negated": True},
                confidence=0.8,
            ),
            StructuredMemory(
                id=82,
                scope="user",
                fact_type="preference",
                key="usage_preference",
                value="不偏好游戏场景",
                value_json={"usage": "gaming", "negated": True},
                confidence=0.8,
            ),
        ],
    )
    context = FakeContextService(prepared)
    registry = FakeToolRegistry(
        {
            "catalog.search": ToolExecutionResult(
                tool_name="catalog.search",
                ok=True,
                output={
                    "result_type": "empty",
                    "products": [],
                    "ranking_strategy": "test",
                    "query_plan": {},
                },
            )
        }
    )

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        knowledge_service=EmptyKnowledgeService(),
        context_service=context,
        tool_registry=registry,
    )

    await runtime.run(ChatRequest(message=prepared.message), user_id=7)

    defaults = registry.calls[0][1]["preference_defaults"]
    assert defaults["excluded_brands"] == ["Logitech"]
    assert defaults["excluded_usage"] == ["gaming"]
    assert context.completed_outcomes[0]["applied_memory_ids"] == [81, 82]


@pytest.mark.asyncio
async def test_working_exclusion_beats_long_term_positive_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=61,
        run_id=71,
        message="推荐鼠标",
        working_memory=WorkingMemoryV2.model_validate(
            {"catalog": {"query_plan": {"excluded_brands": ["Logitech"]}}}
        ),
        memory=[
            StructuredMemory(
                id=83,
                scope="user",
                fact_type="preference",
                key="brand_preference",
                value="偏好 Logitech 品牌",
                value_json={"brand": "Logitech", "negated": False},
                confidence=0.8,
            )
        ],
    )
    context = FakeContextService(prepared)
    registry = FakeToolRegistry(
        {
            "catalog.search": ToolExecutionResult(
                tool_name="catalog.search",
                ok=True,
                output={
                    "result_type": "empty",
                    "products": [],
                    "ranking_strategy": "test",
                    "query_plan": {},
                },
            )
        }
    )

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        knowledge_service=EmptyKnowledgeService(),
        context_service=context,
        tool_registry=registry,
    )

    await runtime.run(ChatRequest(message=prepared.message), user_id=7)

    defaults = registry.calls[0][1]["preference_defaults"]
    assert defaults["brands"] == []
    assert defaults["excluded_brands"] == ["Logitech"]
    assert context.completed_outcomes[0]["applied_memory_ids"] == []
