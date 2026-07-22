import re

from app.agent.artifacts import ensure_task_runtime, ready_tasks
from app.agent.decisions import OrchestratorDecision, PlannedToolCall
from app.agent.intent import classify_intent
from app.agent.routing import RequestRoutePlan, RoutedTask, ready_tool_subqueries

_DIRECT_INTENTS = {
    "catalog_search": "product_recommendation",
    "catalog_compare": "product_recommendation",
    "order_lookup": "order_status",
    "policy_search": "after_sales",
}
_COMPARE_MARKERS = (
    "对比",
    "比较",
    "区别",
    "差异",
    "哪个好",
    "compare",
    "versus",
    " vs ",
)
_FACET_MARKERS = (
    "有哪些品牌",
    "什么品牌",
    "哪些品牌",
    "有哪些品类",
    "哪些品类",
    "规格选项",
    "可选规格",
)


def decision_from_route_capabilities(
    plan: RequestRoutePlan,
    state: dict | None = None,
) -> OrchestratorDecision | None:
    """Build the next ready Tool wave when Router and deterministic vetoes agree.

    The deterministic checks are deliberately a veto rather than an alternative router: a
    disagreement falls back to the Tool Planner and can only reduce acceleration coverage.
    """
    runtime_state = state or {}
    if state is not None and state.get("route_plan"):
        ensure_task_runtime(runtime_state)
        subqueries = ready_tasks(runtime_state)
    else:
        subqueries = ready_tool_subqueries(plan)
    if not subqueries or not all(_direct_capability_is_safe(item) for item in subqueries):
        return None

    calls = [
        PlannedToolCall(
            id=f"router_{item.id}_{item.capability}",
            name=str(item.capability),
            arguments=_default_arguments(item),
            subquery=item.id,
            canonical_query=item.query,
            tool_query=item.query,
        )
        for item in subqueries
    ]
    return OrchestratorDecision(
        type="tool_calls",
        reason="router_capability_direct_wave",
        tool_calls=calls,
    )


def _direct_capability_is_safe(subquery: RoutedTask) -> bool:
    capability = subquery.capability
    compact = re.sub(r"\s+", " ", subquery.query.casefold())
    if capability == "catalog_compare":
        sources = {item.source for item in subquery.input_requirements}
        comparison_followup = not subquery.depends_on and sources == {"comparison_context"}
        if comparison_followup:
            return any(marker in compact for marker in _COMPARE_MARKERS)

    expected_intent = _DIRECT_INTENTS.get(str(capability))
    if expected_intent is None or classify_intent(subquery.query) != expected_intent:
        return False

    if capability == "catalog_search":
        if any(marker in compact for marker in _COMPARE_MARKERS):
            return False
        if any(marker in compact for marker in _FACET_MARKERS):
            return False
    if capability == "catalog_compare":
        sources = {item.source for item in subquery.input_requirements}
        dependent_compare = bool(subquery.depends_on) and sources == {
            "context_product",
            "task_output",
        }
        if not dependent_compare:
            return False
    return True


def _default_arguments(subquery: RoutedTask) -> dict[str, int | str]:
    capability = str(subquery.capability)
    if capability == "catalog_search":
        selector = subquery.result_selector
        if selector is not None and selector.type == "sales_rank":
            return {"limit": selector.rank}
        return {"limit": 3}
    if capability == "catalog_compare":
        return {"limit": 5}
    if capability == "order_lookup":
        return {"limit": 1}
    if capability == "policy_search":
        return {"limit": 3}
    return {}


__all__ = ["decision_from_route_capabilities"]
