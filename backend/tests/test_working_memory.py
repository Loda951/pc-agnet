from typing import cast

import pytest
from langchain_core.messages import HumanMessage

from app.agent.graph import AgentRuntime, _llm_messages
from app.agent.state import AgentState
from app.core.config import Settings


@pytest.mark.asyncio
async def test_product_followup_reuses_previous_search_filters() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))

    state = await runtime._route_intent(
        cast(
            AgentState,
            {
                "message": "换成无线",
                "working_memory": {
                    "current_product_search": {
                        "query": "鼠标",
                        "category": "鼠标",
                        "max_price": "500",
                        "filters": {},
                        "limit": 6,
                    }
                },
            },
        )
    )

    product_search = state["parsed"]["product_search"]
    assert product_search["query"] == "鼠标"
    assert product_search["category"] == "鼠标"
    assert product_search["max_price"] == "500"
    assert product_search["filters"] == {"connection_type": "Wireless"}


@pytest.mark.asyncio
async def test_order_followup_reuses_last_order_id() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))

    state = await runtime._route_intent(
        cast(
            AgentState,
            {
                "message": "这个订单物流到哪了",
                "working_memory": {"last_order_id": 202607020001},
            },
        )
    )

    assert state["intent"] == "order_status"
    assert state["parsed"]["order_id"] == 202607020001


def test_llm_messages_include_working_memory_for_policy_followup() -> None:
    state = cast(
        AgentState,
        {
            "message": "那保修呢",
            "intent": "after_sales",
            "boundary": {
                "classification": "in_scope_auto",
                "reason": "属于 PC 外设商城客服范围，优先进入自动应答流程",
                "display_message": "可自动回答",
            },
            "history": [],
            "memory": [],
            "working_memory": {
                "last_policy_query": "退货政策怎么走",
                "recent_evidence": [
                    {
                        "source_type": "knowledge_document",
                        "source_id": 9001,
                        "title": "测试退货政策",
                    }
                ],
            },
            "evidence": [],
            "products": [],
            "order": None,
        },
    )

    messages = _llm_messages(state)

    current_message = cast(HumanMessage, messages[-1]).content
    assert "working_memory" in current_message
    assert "last_policy_query" in current_message
    assert "退货政策怎么走" in current_message


@pytest.mark.asyncio
async def test_policy_followup_reuses_policy_context_as_after_sales_intent() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))

    state = await runtime._route_intent(
        cast(
            AgentState,
            {
                "message": "那保修呢",
                "working_memory": {"last_policy_query": "退货政策怎么走"},
            },
        )
    )

    assert state["intent"] == "after_sales"


@pytest.mark.asyncio
async def test_product_ordinal_reference_resolves_recent_product() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))

    state = await runtime._route_intent(
        cast(
            AgentState,
            {
                "message": "第二个怎么样",
                "working_memory": {
                    "current_product_search": {
                        "query": "鼠标",
                        "category": "鼠标",
                        "filters": {"connection_type": "Wireless"},
                        "limit": 6,
                    },
                    "recent_products": [
                        {
                            "sku_id": 101,
                            "title": "Logitech G304",
                            "category": "鼠标",
                            "price": "199.00",
                            "stock": 5,
                            "specs": {"connection_type": "Wireless"},
                        },
                        {
                            "sku_id": 102,
                            "title": "Razer Orochi V2",
                            "category": "鼠标",
                            "price": "299.00",
                            "stock": 3,
                            "specs": {"connection_type": "Wireless"},
                        },
                    ],
                },
            },
        )
    )

    assert state["intent"] == "product_recommendation"
    assert state["parsed"]["referenced_product"]["sku_id"] == 102
    assert state["parsed"]["referenced_product"]["title"] == "Razer Orochi V2"


def test_handoff_suggested_action_prefills_order_request_type_and_reason() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))
    state = cast(
        AgentState,
        {
            "message": "这个订单我要退货",
            "boundary": {
                "classification": "human_handoff_required",
                "reason": "涉及售后、订单变更或其他需要人工确认的写操作",
                "display_message": "这个请求需要人工客服确认后处理。",
            },
            "working_memory": {"last_order_id": 202607020001},
            "intent": "after_sales",
        },
    )

    actions = runtime._suggest_actions(state)

    assert actions == [
        {
            "label": "转人工客服",
            "payload": {
                "handoff": True,
                "orderId": 202607020001,
                "requestType": "return",
                "reason": "这个订单我要退货",
            },
        }
    ]


def test_product_reference_fallback_answers_selected_product() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))
    state = cast(
        AgentState,
        {
            "intent": "product_recommendation",
            "parsed": {
                "referenced_product": {
                    "sku_id": 102,
                    "title": "Razer Orochi V2",
                    "category": "鼠标",
                    "price": "299.00",
                    "stock": 3,
                    "specs": {"connection_type": "Wireless", "max_dpi": "18000"},
                }
            },
            "evidence": [],
            "products": [],
        },
    )

    answer = runtime._generate_fallback(state)

    assert "Razer Orochi V2" in answer
    assert "¥299.00" in answer
    assert "库存 3" in answer
    assert "connection_type: Wireless" in answer
