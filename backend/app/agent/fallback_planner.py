"""Deterministic planner used when the orchestration LLM is unavailable."""

from typing import Any

from app.agent.decisions import OrchestratorDecision, PlannedToolCall
from app.agent.intent import classify_intent, extract_order_id
from app.agent.limits import MAX_TOOL_WAVES
from app.agent.responses import (
    _fallback_answer,
    _fallback_catalog_facets_arguments,
    _fallback_unavailable_answer,
    _latest_successful_tool_output,
    _usable_tool_call_ids,
)
from app.agent.route_runtime import _resolve_compare_sku_ids, _resolve_order_id
from app.agent.routing import RoutedSubquery, tool_planning_subqueries
from app.agent.state import AgentState
from app.agent.tool_loop import _clarification_decision, _tool_decision


def fallback_planner_decision(runtime: Any, state: AgentState) -> OrchestratorDecision:
    """Provide an offline decision without bypassing the routed control flow."""
    if state.get("tool_results"):
        order_output = _latest_successful_tool_output(state, "order_lookup")
        order_candidates = (
            order_output.get("candidates")
            if isinstance(order_output, dict)
            else None
        )
        if (
            not state.get("order")
            and state.get("tool_wave_count", 0) < MAX_TOOL_WAVES
            and isinstance(order_candidates, list)
            and order_candidates
            and isinstance(order_candidates[0], dict)
            and order_candidates[0].get("id") is not None
        ):
            return _tool_decision(
                "order_lookup",
                {"order_id": order_candidates[0]["id"], "limit": 1},
            )
        usable_ids = _usable_tool_call_ids(state)
        if usable_ids:
            return OrchestratorDecision(
                type="grounded_response",
                response=_fallback_answer(state),
                reason="llm_not_configured",
                control_action="finish_answer",
                used_tool_call_ids=usable_ids,
            )
        return OrchestratorDecision(
            type="unavailable_response",
            response=_fallback_unavailable_answer(state),
            reason="llm_not_configured",
            control_action="finish_unavailable",
            unavailable_parts=["请求所需的业务信息"],
        )

    routed_subqueries = tool_planning_subqueries(state.get("route_plan"))
    if routed_subqueries:
        return fallback_routed_tool_decision(runtime, state, routed_subqueries)
    return _clarification_decision(
        "我还不能准确判断需要查询的业务信息，请补充具体商品、订单或政策问题。",
        "routed_fallback_without_tool_subquery",
    )

def fallback_routed_tool_decision(
    runtime: Any,
    state: AgentState,
    subqueries: list[RoutedSubquery],
) -> OrchestratorDecision:
    calls: list[PlannedToolCall] = []
    for subquery in subqueries:
        query = subquery.query
        facet_arguments = _fallback_catalog_facets_arguments(query)
        if facet_arguments:
            name = "catalog_facets"
            arguments = facet_arguments
        else:
            intent = classify_intent(query)
            if intent == "product_recommendation":
                compare_sku_ids = _resolve_compare_sku_ids(
                    state["message"], state.get("working_memory", {})
                )
                if compare_sku_ids:
                    name = "catalog_compare"
                    arguments = {
                        "query": query,
                        "sku_ids": compare_sku_ids,
                        "limit": 5,
                    }
                else:
                    name = "catalog_search"
                    arguments = {"query": query, "limit": 3}
            elif intent == "order_status":
                name = "order_lookup"
                arguments = {
                    "query": query,
                    "order_id": _resolve_order_id(
                        query,
                        extract_order_id(query),
                        state.get("working_memory", {}),
                        runtime.memory_service,
                    ),
                    "limit": 1,
                }
            elif intent == "after_sales":
                name = "policy_search"
                arguments = {
                    "query": query,
                    "limit": 3,
                }
            else:
                name = "knowledge_search"
                arguments = {
                    "query": query,
                    "limit": 3,
                }
        calls.append(
            PlannedToolCall(
                id=f"fallback_{subquery.id}_{name}",
                name=name,
                arguments=arguments,
                subquery=subquery.id,
            )
        )
    return OrchestratorDecision(type="tool_calls", tool_calls=calls)
