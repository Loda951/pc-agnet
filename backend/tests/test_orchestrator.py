from typing import cast

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, StateGraph

from app.agent.decisions import decision_from_ai_message
from app.agent.graph import (
    MAX_ORCHESTRATOR_CALLS,
    MAX_TOOL_WAVES,
    AgentRuntime,
    _fallback_answer,
    _has_successful_tool_result,
    _orchestrator_messages,
    _tag_from_decision,
)
from app.agent.prompts import (
    FACT_SOURCE_POLICY,
    MEMORY_CONTEXT_POLICY,
    ORCHESTRATOR_BASE_PROMPT,
    ORCHESTRATOR_SYSTEM_PROMPT,
    ROUTING_EXAMPLES,
    SECURITY_AND_PRIVACY_POLICY,
    TOOL_CALL_PROTOCOL,
    TOOL_SELECTION_RULES,
    build_orchestrator_system_prompt,
    build_orchestrator_user_prompt,
    build_tool_failure_prompt,
)
from app.agent.state import AgentState
from app.core.config import Settings
from app.schemas.chat import ChatRequest
from app.tools.contracts import (
    LLM_SAFE_TOOL_NAMES,
    BoundTool,
    DefaultToolContractProvider,
    RegistryToolExecutor,
    ToolCatalog,
)
from app.tools.schemas import OrderLookupOutput


class FakeChatModel:
    def __init__(self, responses: list[AIMessage]):
        self.responses = responses
        self.call_count = 0

    def bind_tools(self, tools: list[dict]):
        assert len(tools) == 13
        tools_by_name = {tool["function"]["name"]: tool for tool in tools}
        assert set(tools_by_name) >= {
            "catalog_search",
            "finish_answer",
            "finish_unavailable",
            "reject_out_of_scope",
            "request_handoff",
        }
        for name in LLM_SAFE_TOOL_NAMES:
            parameters = tools_by_name[name]["function"]["parameters"]
            assert "subquery" in parameters["properties"]
            assert "subquery" in parameters["required"]
        return self

    async def ainvoke(self, messages):
        assert messages
        response = self.responses[self.call_count]
        self.call_count += 1
        return response


def _control_message(name: str, **arguments) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": f"control-{name}",
                "name": name,
                "args": arguments,
                "type": "tool_call",
            }
        ],
    )


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
    assert examples["有什么鼠标"] == ["catalog_search"]
    assert examples["你们有什么牌子的鼠标"] == ["catalog_facets"]
    assert examples["你们有哪些鼠标品牌"] == ["catalog_facets"]
    assert examples["Logitech 是什么品牌"] == ["knowledge_search"]
    assert examples["Logitech 有哪些鼠标"] == ["catalog_search"]
    assert examples["我的订单发货了吗"] == ["order_lookup"]
    assert examples["商城一般多久发货"] == ["policy_search"]
    assert examples["哪些用户购买过 Logitech 鼠标"] == ["policy_search"]
    assert examples["这单发货了吗，收到后不合适能退吗"] == [
        "order_lookup",
        "policy_search",
    ]


def test_orchestrator_prompt_defines_memory_precedence_and_fact_refresh() -> None:
    policy = "\n".join(MEMORY_CONTEXT_POLICY)

    assert "当前请求中的显式条件和当前 Tool Result > working_memory" in policy
    assert "不得把其中的展示身份当作当前价格或库存" in policy
    assert "只作为缺省" in policy
    assert "不得重复同一个已经成功且信息充分的调用" in ORCHESTRATOR_SYSTEM_PROMPT
    assert "<memory_policy>" in ORCHESTRATOR_SYSTEM_PROMPT


def test_orchestrator_prompt_preserves_sku_and_spu_sales_semantics() -> None:
    assert "sku_sales_count 是当前 SKU 的销量" in ORCHESTRATOR_SYSTEM_PROMPT
    assert "sales_count 是该 SPU 下所有 SKU" in ORCHESTRATOR_SYSTEM_PROMPT
    assert "不得把它当成单个版本销量" in ORCHESTRATOR_SYSTEM_PROMPT
    rules = "\n".join(TOOL_SELECTION_RULES)
    examples = {item["request"]: item["decision"] for item in ROUTING_EXAMPLES}
    assert "sku_sales_count 表示当前 SKU" in rules
    assert "不得用当前累计销量推断趋势" in ORCHESTRATOR_SYSTEM_PROMPT
    assert examples["G502 黑色版本当前销量多少"] == ["catalog_search"]
    assert examples["这两个颜色哪个更畅销"] == ["catalog_compare"]
    assert examples["鼠标近三个月销量趋势"] == ["catalog_search"]


def test_orchestrator_prompt_requires_native_control_action_without_type_protocol() -> None:
    assert "不要直接输出正文、TYPE 头" in ORCHESTRATOR_SYSTEM_PROMPT
    assert "第一行必须且只能输出 TYPE" not in ORCHESTRATOR_SYSTEM_PROMPT


def test_orchestrator_prompt_is_split_into_agent_runtime_and_response_blocks() -> None:
    expected_blocks = [
        "agent_identity",
        "primary_objective",
        "runtime_model",
        "decision_policy",
        "instruction_priority",
        "scope_and_safety",
        "security_and_privacy_policy",
        "fact_sources",
        "memory_policy",
        "subquery_protocol",
        "tool_routing",
        "tool_call_protocol",
        "tool_loop_policy",
        "control_action_policy",
        "terminal_response_contract",
        "response_style",
    ]

    for block in expected_blocks:
        assert f"<{block}>" in ORCHESTRATOR_SYSTEM_PROMPT
        assert f"</{block}>" in ORCHESTRATOR_SYSTEM_PROMPT


def test_orchestrator_prompt_defines_sensitive_customer_data_boundary() -> None:
    assert "其他用户的身份与联系方式" in SECURITY_AND_PRIVACY_POLICY
    assert "不得使用\n  `reject_out_of_scope`" in SECURITY_AND_PRIVACY_POLICY
    assert "不得调用 `order_lookup`" in SECURITY_AND_PRIVACY_POLICY
    assert "调用 `policy_search`" in SECURITY_AND_PRIVACY_POLICY
    assert "商品级公开统计" in SECURITY_AND_PRIVACY_POLICY
    assert SECURITY_AND_PRIVACY_POLICY in ORCHESTRATOR_SYSTEM_PROMPT


def test_orchestrator_prompt_enforces_schema_fidelity_and_error_recovery() -> None:
    assert "不得翻译字段名、创造别名" in TOOL_CALL_PROTOCOL
    assert "[Tool input]" in TOOL_CALL_PROTOCOL
    assert "query-first 工具" in TOOL_CALL_PROTOCOL
    assert "不要生成或覆盖 Tool 内部查询计划" in TOOL_CALL_PROTOCOL
    assert "invalid_catalog_plan" in TOOL_CALL_PROTOCOL
    assert "code=invalid_input" in TOOL_CALL_PROTOCOL
    assert "code=execution_error" in TOOL_CALL_PROTOCOL
    assert "不得修改 key/value 猜测原因" in TOOL_CALL_PROTOCOL
    assert "reused_from_tool_call_id" in TOOL_CALL_PROTOCOL
    assert TOOL_CALL_PROTOCOL in ORCHESTRATOR_SYSTEM_PROMPT


def test_empty_tool_result_does_not_load_failure_recovery_prompt() -> None:
    tool_waves = [
        {
            "calls": [
                {
                    "id": "call-1",
                    "name": "catalog_search",
                    "arguments": {"query": "不存在的产品"},
                }
            ],
            "results": [
                {
                    "tool_call_id": "call-1",
                    "name": "catalog_search",
                    "execution": {
                        "ok": True,
                        "output": {"result_type": "empty", "products": []},
                    },
                }
            ],
        }
    ]

    assert build_tool_failure_prompt(tool_waves=tool_waves) == ""
    assert build_orchestrator_system_prompt(tool_waves=tool_waves) == ORCHESTRATOR_BASE_PROMPT


def test_timeout_loads_only_relevant_failure_recovery_rule() -> None:
    tool_waves = [
        {
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
                        "ok": False,
                        "error": {
                            "code": "timeout",
                            "retryable": True,
                            "recommended_action": "retry_once",
                        },
                    },
                }
            ],
        }
    ]

    prompt = build_tool_failure_prompt(tool_waves=tool_waves)

    assert "<tool_failure_recovery>" in prompt
    assert "`retry_once`" in prompt
    assert "`replan_arguments`" not in prompt
    assert "catalog_search：code=timeout" in prompt
    assert "当前没有成功 Tool Result" in prompt


def test_repeated_timeout_marks_retry_limit_and_mixed_success() -> None:
    timeout_result = {
        "name": "catalog_search",
        "execution": {
            "ok": False,
            "error": {
                "code": "timeout",
                "retryable": True,
                "recommended_action": "retry_once",
            },
        },
    }
    tool_waves = [
        {
            "calls": [
                {
                    "id": "call-1",
                    "name": "catalog_search",
                    "arguments": {"query": "无线鼠标"},
                },
                {
                    "id": "call-knowledge",
                    "name": "knowledge_search",
                    "arguments": {"query": "DPI"},
                },
            ],
            "results": [
                {"tool_call_id": "call-1", **timeout_result},
                {
                    "tool_call_id": "call-knowledge",
                    "name": "knowledge_search",
                    "execution": {
                        "ok": True,
                        "output": {"result_type": "documents", "documents": []},
                    },
                },
            ],
        },
        {
            "calls": [
                {
                    "id": "call-2",
                    "name": "catalog_search",
                    "arguments": {"query": "无线鼠标"},
                }
            ],
            "results": [{"tool_call_id": "call-2", **timeout_result}],
        },
    ]

    prompt = build_tool_failure_prompt(tool_waves=tool_waves)

    assert "相同调用与错误已失败 2 次，已达到重试上限" in prompt
    assert "当前没有成功 Tool Result" in prompt


def test_user_prompt_separates_request_execution_and_memory_data() -> None:
    prompt = build_orchestrator_user_prompt(
        message="换成无线",
        tool_wave_count=1,
        orchestrator_call_count=2,
        memory_context={"working_memory": {"catalog": {"category": "mouse"}}},
    )

    assert '<current_request>\n"换成无线"\n</current_request>' in prompt
    assert '"completed_tool_waves": 1' in prompt
    assert '"remaining_tool_waves": 1' in prompt
    assert '"remaining_orchestrator_calls": 1' in prompt
    assert '"must_terminate_now": false' in prompt
    assert "<memory_context>" in prompt


def test_user_prompt_marks_final_orchestrator_call_as_terminal_only() -> None:
    prompt = build_orchestrator_user_prompt(
        message="继续查",
        tool_wave_count=2,
        orchestrator_call_count=3,
    )

    assert '"remaining_tool_waves": 0' in prompt
    assert '"remaining_orchestrator_calls": 0' in prompt
    assert '"must_terminate_now": true' in prompt


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

    decision = decision_from_ai_message(message, has_successful_tool_results=False)

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
                    "args": {"query": "有哪些鼠标品牌"},
                    "type": "tool_call",
                }
            ],
        ),
        has_successful_tool_results=False,
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
        has_successful_tool_results=True,
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
        "limit": 20,
    }


def test_fallback_answer_renders_catalog_facets_result() -> None:
    state = cast(
        AgentState,
        {
            "tool_results": [
                {
                    "tool_call_id": "call-facets",
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


def test_finish_direct_control_action_is_parsed() -> None:
    decision = decision_from_ai_message(_control_message("finish_direct", response="你好。"))

    assert decision.type == "direct_response"
    assert decision.control_action == "finish_direct"
    assert decision.response == "你好。"


def test_finish_answer_control_action_declares_evidence_ids() -> None:
    decision = decision_from_ai_message(
        _control_message(
            "finish_answer",
            response="目前目录中有以下鼠标。",
            used_tool_call_ids=["call-1"],
        )
    )

    assert decision.type == "grounded_response"
    assert decision.used_tool_call_ids == ["call-1"]
    assert decision.response == "目前目录中有以下鼠标。"


def test_request_handoff_control_action_is_parsed() -> None:
    decision = decision_from_ai_message(
        _control_message(
            "request_handoff",
            response="这个操作需要人工客服处理。",
            reason="需要修改订单状态",
            requested_action="取消最近订单",
        )
    )

    assert decision.type == "handoff"
    assert decision.control_action == "request_handoff"
    assert decision.reason == "需要修改订单状态"
    assert decision.requested_action == "取消最近订单"


def test_empty_terminal_response_is_rejected() -> None:
    with pytest.raises(ValueError, match="neither tool calls nor a control action"):
        decision_from_ai_message(
            AIMessage(content=""),
        )


@pytest.mark.asyncio
async def test_orchestrator_accepts_complete_terminal_response() -> None:
    runtime = AgentRuntime(
        cast(object, None),
        Settings(llm_api_key=""),
        chat_model=FakeChatModel(
            [_control_message("finish_direct", response="你好，我是商城客服。")]
        ),
    )
    workflow = StateGraph(AgentState)
    workflow.add_node("orchestrate", runtime._orchestrate)
    workflow.set_entry_point("orchestrate")
    workflow.add_edge("orchestrate", END)

    result = await workflow.compile().ainvoke(
        {
            "message": "你是谁？",
            "history": [],
            "tool_results": [],
            "tool_waves": [],
            "tool_wave_count": 0,
            "orchestrator_call_count": 0,
        }
    )

    assert result["decision"]["type"] == "direct_response"
    assert result["decision"]["response"] == "你好，我是商城客服。"


@pytest.mark.asyncio
async def test_deterministic_boundary_skips_llm_for_handoff() -> None:
    model = FakeChatModel([AIMessage(content="不应该调用模型")])
    runtime = AgentRuntime(
        cast(object, None),
        Settings(llm_api_key=""),
        chat_model=model,
    )

    result = await runtime._orchestrate(
        cast(
            AgentState,
            {
                "message": "帮我取消订单",
                "tool_results": [],
                "tool_wave_count": 0,
                "orchestrator_call_count": 0,
            },
        )
    )

    assert result["decision"]["type"] == "handoff"
    assert result["boundary"]["classification"] == "human_handoff_required"
    assert model.call_count == 0


@pytest.mark.asyncio
async def test_native_tool_call_is_parsed_from_complete_message() -> None:
    runtime = AgentRuntime(
        cast(object, None),
        Settings(llm_api_key=""),
        chat_model=FakeChatModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "catalog_search",
                            "args": {
                                "query": "无线鼠标",
                                "subquery": "推荐无线鼠标",
                            },
                            "id": "call-1",
                            "type": "tool_call",
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

    result = await workflow.compile().ainvoke(
        {
            "message": "推荐无线鼠标",
            "history": [],
            "tool_results": [],
            "tool_waves": [],
            "tool_wave_count": 0,
            "orchestrator_call_count": 0,
        }
    )

    decision = result["decision"]
    assert decision["type"] == "tool_calls"
    assert decision["tool_calls"][0]["name"] == "catalog_search"
    assert decision["tool_calls"][0]["subquery"] == "推荐无线鼠标"
    assert "subquery" not in decision["tool_calls"][0]["arguments"]


@pytest.mark.asyncio
async def test_tool_loop_accepts_finish_answer_control_on_second_call() -> None:
    runtime = AgentRuntime(
        cast(object, None),
        Settings(llm_api_key=""),
        chat_model=FakeChatModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "catalog_search",
                            "args": {"query": "无线鼠标"},
                            "id": "call-1",
                            "type": "tool_call",
                        }
                    ],
                ),
                _control_message(
                    "finish_answer",
                    response="根据查询结果，推荐这三款无线鼠标。",
                    used_tool_call_ids=["call-1"],
                ),
            ]
        ),
    )
    state = cast(
        AgentState,
        {
            "message": "推荐无线鼠标",
            "history": [],
            "tool_results": [],
            "tool_waves": [],
            "tool_wave_count": 0,
            "orchestrator_call_count": 0,
        },
    )

    first = await runtime._orchestrate(state)
    assert first["decision"]["type"] == "tool_calls"

    first["tool_wave_count"] = 1
    first["tool_results"] = [
        {
            "tool_call_id": "call-1",
            "name": "catalog_search",
            "execution": {
                "ok": True,
                "output": {
                    "result_type": "products",
                    "products": [{"sku_id": 1}],
                },
            },
        }
    ]
    second = await runtime._orchestrate(first)

    assert second["orchestrator_call_count"] == 2
    assert second["decision"]["type"] == "grounded_response"
    assert second["decision"]["response"] == "根据查询结果，推荐这三款无线鼠标。"


@pytest.mark.asyncio
async def test_invalid_terminal_after_usable_tool_uses_immediate_grounded_fallback() -> None:
    runtime = AgentRuntime(
        cast(object, None),
        Settings(llm_api_key=""),
        chat_model=FakeChatModel([AIMessage(content=""), AIMessage(content="")]),
    )

    state = cast(
        AgentState,
        {
            "message": "你们有什么牌子的鼠标",
            "history": [],
            "tool_waves": [],
            "tool_wave_count": 1,
            "orchestrator_call_count": 1,
            "intent": "catalog_facets",
            "tool_results": [
                {
                    "tool_call_id": "call-facets-terminal",
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
            ],
        },
    )

    result = await runtime._orchestrate(state)

    assert _has_successful_tool_result(state) is True
    assert result["decision"]["reason"] == "invalid_orchestrator_response:ValueError"
    guarded = await runtime._terminal_guard(result)

    assert guarded["terminal_guard_status"] == "fallback"
    assert guarded["decision"]["type"] == "grounded_response"
    assert runtime.orchestrator.call_count == 1
    assert "Logitech（12 条 SKU 记录）" in guarded["decision"]["response"]


@pytest.mark.asyncio
async def test_run_stream_sends_one_validated_answer_delta_before_done() -> None:
    class ProgressRuntime(AgentRuntime):
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

    runtime = ProgressRuntime(
        cast(object, None),
        Settings(llm_api_key=""),
        chat_model=FakeChatModel(
            [_control_message("finish_direct", response="完整校验后的回答")]
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
    assert deltas == ["完整校验后的回答"]
    assert events[-1]["response"]["answer"] == "完整校验后的回答"


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
                            "subquery": "推荐无线鼠标",
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
    assert isinstance(messages[-2], AIMessage)
    assert messages[-2].tool_calls[0]["args"]["subquery"] == "推荐无线鼠标"
    assert messages[-1].tool_call_id == "call-1"
    assert '"result_type": "empty"' in str(messages[-1].content)
    assert "\n<tool_failure_recovery>\n" not in str(messages[0].content)


def test_orchestrator_messages_load_failure_recovery_into_first_system_message() -> None:
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
                                "ok": False,
                                "error": {
                                    "code": "timeout",
                                    "retryable": True,
                                    "recommended_action": "retry_once",
                                },
                            },
                        }
                    ],
                }
            ],
        },
    )

    messages = _orchestrator_messages(state, call_count=2)

    assert "\n<tool_failure_recovery>\n" in str(messages[0].content)
    assert "catalog_search：code=timeout" in str(messages[0].content)
    assert isinstance(messages[-1], ToolMessage)


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
        has_successful_tool_results=True,
    )

    guarded = runtime._validate_decision_budget(
        state,
        decision,
        MAX_ORCHESTRATOR_CALLS,
    )

    assert guarded.type == "clarification"
    assert guarded.tool_calls == []
    assert "处理上限" not in guarded.response
    assert "补充具体商品" in guarded.response


def test_orchestrator_prompt_uses_tool_specific_sufficiency_without_internal_limits() -> None:
    assert "`catalog_search` 返回至少一个" in ORCHESTRATOR_SYSTEM_PROMPT
    assert "`catalog_compare` 返回至少两款" in ORCHESTRATOR_SYSTEM_PROMPT
    assert "至少一篇能直接支持核心问题" in ORCHESTRATOR_SYSTEM_PROMPT
    assert "同一套 `finish_answer`" in ORCHESTRATOR_SYSTEM_PROMPT
    assert "最终回复不得提及调用次数" in ORCHESTRATOR_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_order_user_id_is_injected_by_runtime() -> None:
    captured_input: dict | None = None
    contract = DefaultToolContractProvider().get_contract("order_lookup")
    assert contract is not None

    async def handler(request) -> OrderLookupOutput:
        nonlocal captured_input
        captured_input = request.model_dump(mode="json", exclude_none=True)
        return OrderLookupOutput(result_type="not_found")

    executor = RegistryToolExecutor(
        cast(object, None),
        Settings(llm_api_key=""),
        catalog=ToolCatalog([BoundTool(contract, handler)]),
    )

    result = await executor.execute(contract, {"order_id": 42}, {"user_id": 7})

    assert result.ok is True
    assert captured_input == {"user_id": 7, "order_id": 42, "limit": 5}
