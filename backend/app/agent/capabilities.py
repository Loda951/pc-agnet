import re

from app.agent.decisions import OrchestratorDecision, PlannedToolCall
from app.agent.intent import classify_intent
from app.agent.routing import RequestRoutePlan, RoutedSubquery, tool_planning_subqueries

_DIRECT_INTENTS = {
    "catalog_search": "product_recommendation",
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
) -> OrchestratorDecision | None:
    """Build a first Tool wave only when Router and deterministic vetoes agree.

    The deterministic checks are deliberately a veto rather than an alternative router: a
    disagreement falls back to the Tool Planner and can only reduce acceleration coverage.
    """
    subqueries = tool_planning_subqueries(plan)
    if not subqueries or not all(_direct_capability_is_safe(item) for item in subqueries):
        return None

    calls = [
        PlannedToolCall(
            id=f"router_{item.id}_{item.capability}",
            name=str(item.capability),
            arguments=_default_arguments(str(item.capability)),
            subquery=item.id,
        )
        for item in subqueries
    ]
    return OrchestratorDecision(
        type="tool_calls",
        reason="router_capability_direct_wave",
        tool_calls=calls,
    )


def _direct_capability_is_safe(subquery: RoutedSubquery) -> bool:
    capability = subquery.capability
    expected_intent = _DIRECT_INTENTS.get(str(capability))
    if expected_intent is None or classify_intent(subquery.query) != expected_intent:
        return False

    compact = re.sub(r"\s+", " ", subquery.query.casefold())
    if capability == "catalog_search":
        if any(marker in compact for marker in _COMPARE_MARKERS):
            return False
        if any(marker in compact for marker in _FACET_MARKERS):
            return False
    return True


def _default_arguments(capability: str) -> dict[str, int]:
    if capability == "catalog_search":
        return {"limit": 3}
    if capability == "order_lookup":
        return {"limit": 1}
    if capability == "policy_search":
        return {"limit": 3}
    return {}


__all__ = ["decision_from_route_capabilities"]
