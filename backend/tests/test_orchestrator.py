from typing import cast

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.graph import END, StateGraph

from app.agent.decisions import TerminalResponseStreamParser, decision_from_ai_message
from app.agent.graph import (
    MAX_ORCHESTRATOR_CALLS,
    MAX_TOOL_WAVES,
    AgentRuntime,
    _fallback_answer,
    _orchestrator_messages,
    _tag_from_decision,
)
from app.agent.prompts import (
    FACT_SOURCE_POLICY,
    ORCHESTRATOR_SYSTEM_PROMPT,
    ROUTING_EXAMPLES,
    TOOL_SELECTION_RULES,
)
from app.agent.state import AgentState
from app.core.config import Settings
from app.schemas.chat import ChatRequest
from app.tools.contracts import DefaultToolContractProvider, RegistryToolExecutor
from app.tools.schemas import ToolExecutionResult


class FakeStreamingChatModel:
    def __init__(self, chunks: list[AIMessageChunk]):
        self.chunks = chunks

    def bind_tools(self, tools: list[dict]):
        assert len(tools) == 6
        return self

    async def astream(self, messages):
        assert messages
        for chunk in self.chunks:
            yield chunk


def test_orchestrator_prompt_separates_fact_sources_without_repeating_schemas() -> None:
    structured = FACT_SOURCE_POLICY["structured_business_facts"]
    documents = FACT_SOURCE_POLICY["document_evidence"]

    assert structured["tools"] == [
        "catalog_search",
        "catalog_compare",
        "catalog_facets",
        "order_lookup",
    ]
    assert documents["tools"] == ["policy_search", "knowledge_search"]
    assert "字段级事实" in structured["use_for"]
    assert "文档证据" in documents["authority"]
    assert "parameters" not in ORCHESTRATOR_SYSTEM_PROMPT


def test_orchestrator_prompt_covers_high_confusion_tool_boundaries() -> None:
    rules = "\n".join(TOOL_SELECTION_RULES)
    examples = {item["request"]: item["decision"] for item in ROUTING_EXAMPLES}

    assert "具体 SKU" in rules
    assert "一般性的配送" in rules
    assert examples["你们有哪些鼠标品牌"] == ["catalog_facets"]
    assert examples["Logitech 是什么品牌"] == ["knowledge_search"]
    assert examples["Logitech 有哪些鼠标"] == ["catalog_search"]
    assert examples["我的订单发货了吗"] == ["order_lookup"]
    assert examples["商城一般多久发货"] == ["policy_search"]
    assert examples["这单发货了吗，收到后不合适能退吗"] == [
        "order_lookup",
        "policy_search",
    ]


def test_native_tool_calls_are_normalized_as_one_wave() -> None:
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "catalog-call",
                "name": "catalog_search",
                "args": {"query": "无线鼠标"},
                "type": "tool_call",
            },
            {
                "id": "policy-call",
                "name": "policy_search",
                "args": {"query": "鼠标退货政策"},
                "type": "tool_call",
            },
        ],
    )

    decision = decision_from_ai_message(message, has_tool_results=False)

    assert decision.type == "tool_calls"
    assert [call.name for call in decision.tool_calls] == [
        "catalog_search",
        "policy_search",
    ]
    assert _tag_from_decision(decision, None) == "catalog_search + policy_search"


def test_catalog_facets_tag_is_preserved_across_tool_waves() -> None:
    facets = decision_from_ai_message(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "facets-call",
                    "name": "catalog_facets",
                    "args": {"query": "有哪些鼠标品牌", "facet": "brand"},
                    "type": "tool_call",
                }
            ],
        ),
        has_tool_results=False,
    )
    search = decision_from_ai_message(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "search-call",
                    "name": "catalog_search",
                    "args": {"query": "Logitech 鼠标"},
                    "type": "tool_call",
                }
            ],
        ),
        has_tool_results=True,
    )

    tag = _tag_from_decision(facets, None)

    assert _tag_from_decision(search, tag) == "catalog_facets + catalog_search"


def test_fallback_orchestrator_routes_catalog_facets_questions() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))

    decision = runtime._fallback_orchestrator_decision(
        cast(AgentState, {"message": "你们有哪些鼠标品牌？", "tool_results": []})
    )

    assert decision.type == "tool_calls"
    assert decision.tool_calls[0].name == "catalog_facets"
    assert decision.tool_calls[0].arguments == {
        "query": "你们有哪些鼠标品牌？",
        "facet": "brand",
        "limit": 20,
    }


def test_fallback_answer_renders_catalog_facets_result() -> None:
    state = cast(
        AgentState,
        {
            "tool_results": [
                {
                    "name": "catalog_facets",
                    "execution": {
                        "ok": True,
                        "output": {
                            "result_type": "facets",
                            "facet": "brand",
                            "items": [
                                {"value": "Logitech", "count": 12},
                                {"value": "Razer", "count": 8},
                            ],
                        },
                    },
                }
            ]
        },
    )

    answer = _fallback_answer(state)

    assert "Logitech（12 条 SKU 记录）" in answer
    assert "Razer（8 条 SKU 记录）" in answer


def test_terminal_json_is_parsed_without_tool_call() -> None:
    message = AIMessage(
        content=(
            '{"type":"clarification","response":"请告诉我预算范围。",'
            '"reason":"missing_budget"}'
        )
    )

    decision = decision_from_ai_message(message, has_tool_results=False)

    assert decision.type == "clarification"
    assert decision.response == "请告诉我预算范围。"
    assert decision.tool_calls == []


def test_type_header_parser_streams_only_body_after_complete_header() -> None:
    parser = TerminalResponseStreamParser()

    assert parser.feed("TYPE: grounded_") == []
    assert parser.feed("response\n") == []
    assert parser.feed("\n第一段") == ["第一段"]
    assert parser.feed("，第二段") == ["，第二段"]

    decision = parser.finish()
    assert decision.type == "grounded_response"
    assert decision.response == "第一段，第二段"


def test_type_header_parser_suppresses_template_body() -> None:
    parser = TerminalResponseStreamParser()

    assert parser.feed("TYPE: handoff\n\n不应展示的模型正文") == []

    decision = parser.finish()
    assert decision.type == "handoff"
    assert decision.response == ""


@pytest.mark.asyncio
async def test_orchestrator_emits_real_deltas_before_node_update() -> None:
    runtime = AgentRuntime(
        cast(object, None),
        Settings(llm_api_key=""),
        chat_model=FakeStreamingChatModel(
            [
                AIMessageChunk(content="TYPE: direct_"),
                AIMessageChunk(content="response\n\n你"),
                AIMessageChunk(content="好，我是商城客服。"),
            ]
        ),
    )
    workflow = StateGraph(AgentState)
    workflow.add_node("orchestrate", runtime._orchestrate)
    workflow.set_entry_point("orchestrate")
    workflow.add_edge("orchestrate", END)

    events = [
        event
        async for event in workflow.compile().astream(
            {
                "message": "你是谁？",
                "history": [],
                "tool_results": [],
                "tool_waves": [],
                "tool_wave_count": 0,
                "orchestrator_call_count": 0,
            },
            stream_mode=["custom", "updates"],
        )
    ]

    modes = [mode for mode, _ in events]
    deltas = [
        payload["delta"]
        for mode, payload in events
        if mode == "custom" and payload.get("kind") == "response_delta"
    ]
    assert modes.index("custom") < modes.index("updates")
    assert "".join(deltas) == "你好，我是商城客服。"
    final_update = next(payload for mode, payload in events if mode == "updates")
    assert final_update["orchestrate"]["decision"]["response"] == "你好，我是商城客服。"


@pytest.mark.asyncio
async def test_native_tool_call_does_not_emit_response_delta() -> None:
    runtime = AgentRuntime(
        cast(object, None),
        Settings(llm_api_key=""),
        chat_model=FakeStreamingChatModel(
            [
                AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "catalog_search",
                            "args": '{"query":"无线鼠标"}',
                            "id": "call-1",
                            "index": 0,
                            "type": "tool_call_chunk",
                        }
                    ],
                )
            ]
        ),
    )
    workflow = StateGraph(AgentState)
    workflow.add_node("orchestrate", runtime._orchestrate)
    workflow.set_entry_point("orchestrate")
    workflow.add_edge("orchestrate", END)

    events = [
        event
        async for event in workflow.compile().astream(
            {
                "message": "推荐无线鼠标",
                "history": [],
                "tool_results": [],
                "tool_waves": [],
                "tool_wave_count": 0,
                "orchestrator_call_count": 0,
            },
            stream_mode=["custom", "updates"],
        )
    ]

    assert not any(
        mode == "custom" and payload.get("kind") == "response_delta"
        for mode, payload in events
    )
    final_update = next(payload for mode, payload in events if mode == "updates")
    decision = final_update["orchestrate"]["decision"]
    assert decision["type"] == "tool_calls"
    assert decision["tool_calls"][0]["name"] == "catalog_search"


@pytest.mark.asyncio
async def test_run_stream_forwards_model_chunks_before_done() -> None:
    class StreamingRuntime(AgentRuntime):
        async def _test_load_context(self, state: AgentState) -> AgentState:
            state["conversation_id"] = 10
            state["run_id"] = 20
            state["history"] = []
            return state

        async def _test_persist(self, state: AgentState) -> AgentState:
            return state

        def _build_graph(self):
            workflow = StateGraph(AgentState)
            workflow.add_node("load_context", self._test_load_context)
            workflow.add_node("orchestrate", self._orchestrate)
            workflow.add_node("finalize_response", self._finalize_response)
            workflow.add_node("persist_turn", self._test_persist)
            workflow.set_entry_point("load_context")
            workflow.add_edge("load_context", "orchestrate")
            workflow.add_edge("orchestrate", "finalize_response")
            workflow.add_edge("finalize_response", "persist_turn")
            workflow.add_edge("persist_turn", END)
            return workflow.compile()

    runtime = StreamingRuntime(
        cast(object, None),
        Settings(llm_api_key=""),
        chat_model=FakeStreamingChatModel(
            [
                AIMessageChunk(content="TYPE: direct_response\n\n流"),
                AIMessageChunk(content="式回答"),
            ]
        ),
    )

    events = [
        event
        async for event in runtime.run_stream(
            ChatRequest(message="你是谁？"),
            user_id=1,
        )
    ]

    event_types = [event["type"] for event in events]
    deltas = [event["delta"] for event in events if event["type"] == "delta"]
    assert event_types[0] == "run_started"
    assert event_types.index("delta") < event_types.index("done")
    assert "".join(deltas) == "流式回答"
    assert events[-1]["response"]["answer"] == "流式回答"


def test_tool_results_are_reconstructed_as_tool_messages() -> None:
    state = cast(
        AgentState,
        {
            "message": "推荐无线鼠标",
            "history": [],
            "tool_wave_count": 1,
            "tool_waves": [
                {
                    "wave": 1,
                    "calls": [
                        {
                            "id": "call-1",
                            "name": "catalog_search",
                            "arguments": {"query": "无线鼠标"},
                        }
                    ],
                    "results": [
                        {
                            "tool_call_id": "call-1",
                            "name": "catalog_search",
                            "execution": {
                                "tool_name": "catalog.search",
                                "ok": True,
                                "output": {"result_type": "empty"},
                                "error": None,
                            },
                        }
                    ],
                }
            ],
        },
    )

    messages = _orchestrator_messages(state, call_count=2)

    assert isinstance(messages[-1], ToolMessage)
    assert messages[-1].tool_call_id == "call-1"
    assert '"result_type": "empty"' in str(messages[-1].content)


def test_third_orchestrator_call_cannot_start_third_tool_wave() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))
    state = cast(
        AgentState,
        {
            "tool_wave_count": MAX_TOOL_WAVES,
        },
    )
    decision = decision_from_ai_message(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call-3",
                    "name": "catalog_search",
                    "args": {"query": "鼠标"},
                    "type": "tool_call",
                }
            ],
        ),
        has_tool_results=True,
    )

    guarded = runtime._validate_decision_budget(
        state,
        decision,
        MAX_ORCHESTRATOR_CALLS,
    )

    assert guarded.type == "clarification"
    assert guarded.tool_calls == []
    assert "处理上限" in guarded.response


@pytest.mark.asyncio
async def test_order_user_id_is_injected_by_runtime() -> None:
    class CapturingRegistry:
        def __init__(self):
            self.input_data: dict | None = None

        async def execute(self, name: str, input_data: dict) -> ToolExecutionResult:
            self.input_data = input_data
            return ToolExecutionResult(
                tool_name=name,
                ok=True,
                output={"result_type": "not_found", "order": None, "candidates": []},
            )

    registry = CapturingRegistry()
    executor = RegistryToolExecutor(
        cast(object, None),
        Settings(llm_api_key=""),
        registry=cast(object, registry),
    )
    contract = DefaultToolContractProvider().get_contract("order_lookup")
    assert contract is not None

    await executor.execute(contract, {"order_id": 42}, {"user_id": 7})

    assert registry.input_data == {"order_id": 42, "limit": 5, "user_id": 7}
