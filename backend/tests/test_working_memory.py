from typing import cast

from langchain_core.messages import HumanMessage

from app.agent.decisions import OrchestratorDecision
from app.agent.graph import AgentRuntime, _orchestrator_messages
from app.agent.state import AgentState
from app.core.config import Settings


def test_orchestrator_messages_include_current_request_context() -> None:
    state = cast(
        AgentState,
        {
            "message": "Recommend a wireless mouse",
            "history": [],
            "tool_wave_count": 1,
            "tool_waves": [],
        },
    )

    messages = _orchestrator_messages(state, call_count=2)

    current_message = cast(HumanMessage, messages[-1]).content
    assert "Recommend a wireless mouse" in str(current_message)
    assert "completed_tool_waves" in str(current_message)
    assert "current_orchestrator_call" in str(current_message)


def test_orchestrator_messages_reconstruct_tool_observations() -> None:
    state = cast(
        AgentState,
        {
            "message": "Recommend a wireless mouse",
            "history": [],
            "tool_wave_count": 1,
            "tool_waves": [
                {
                    "wave": 1,
                    "calls": [
                        {
                            "id": "call-1",
                            "name": "catalog_search",
                            "arguments": {"query": "wireless mouse"},
                        }
                    ],
                    "results": [
                        {
                            "tool_call_id": "call-1",
                            "name": "catalog_search",
                            "execution": {
                                "tool_name": "catalog_search",
                                "ok": True,
                                "output": {"result_type": "empty", "products": []},
                                "error": None,
                            },
                        }
                    ],
                }
            ],
        },
    )

    messages = _orchestrator_messages(state, call_count=2)

    assert any(getattr(message, "tool_call_id", None) == "call-1" for message in messages)
    assert any('"result_type": "empty"' in str(message.content) for message in messages)


def test_fallback_orchestrator_routes_order_lookup_without_old_route_intent() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))
    state = cast(AgentState, {"message": "Where is order 202607020001?"})

    decision = runtime._fallback_orchestrator_decision(state)

    assert decision.type == "tool_calls"
    assert decision.tool_calls[0].name == "order_lookup"
    assert decision.tool_calls[0].arguments["order_id"] == 202607020001


def test_fallback_orchestrator_routes_policy_search_without_old_route_intent() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))
    state = cast(AgentState, {"message": "return policy"})

    decision = runtime._fallback_orchestrator_decision(state)

    assert decision.type == "tool_calls"
    assert decision.tool_calls[0].name in {"policy_search", "knowledge_search"}


def test_fallback_orchestrator_direct_identity_response() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))
    state = cast(AgentState, {"message": "what can you do?"})

    decision = runtime._fallback_orchestrator_decision(state)

    assert isinstance(decision, OrchestratorDecision)
    assert decision.type in {"direct_response", "tool_calls"}
