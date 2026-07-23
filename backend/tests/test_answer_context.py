from typing import Any, cast

import pytest

from app.agent.answer_context import build_answer_context
from app.agent.decisions import OrchestratorDecision
from app.agent.graph import AgentRuntime
from app.agent.outcomes import validate_terminal_decision
from app.agent.responses import LATE_HANDOFF_CONFIRMATION
from app.agent.state import AgentState
from app.core.config import Settings


def _goal(*tasks: dict[str, Any]) -> dict[str, Any]:
    return {
        "rewritten_query": "处理用户请求",
        "subqueries": [
            {
                "id": "goal_1",
                "query": "处理用户请求",
                "disposition": "tool_planning",
                "reason_code": "test",
                "tasks": list(tasks),
            }
        ],
    }


def _task(
    task_id: str,
    query: str,
    *,
    produces: str,
    capability: str,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "goal_id": "goal_1",
        "canonical_query": query,
        "depends_on": [],
        "input_requirements": [],
        "produces": produces,
        "answer_role": "user_facing",
        "capability": capability,
    }


def _artifact(
    task_id: str,
    call_id: str,
    tool_name: str,
    artifact_type: str,
    *,
    usable: bool,
    value: Any,
    reason: str,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "goal_id": "goal_1",
        "artifact_type": artifact_type,
        "usable": usable,
        "value": value,
        "evidence": [{"source_tool_call_id": call_id}] if usable else [],
        "source_tool_call_id": call_id,
        "source_tool_name": tool_name,
        "extractor": "deterministic",
        "reason": reason,
    }


def _ledger(
    task_id: str,
    call_id: str,
    tool_name: str,
    outcome: str,
    *,
    usable: bool,
) -> dict[str, Any]:
    return {
        "tool_call_id": call_id,
        "tool_name": tool_name,
        "subquery": task_id,
        "status": "ready_to_answer" if usable else "unavailable",
        "outcome": outcome,
        "has_usable_information": usable,
        "reason": f"test:{outcome}",
        "wave": 1,
        "arguments": {},
    }


def test_facets_answer_context_requires_brand_values_not_count_sum() -> None:
    task = _task(
        "task_1",
        "查询键盘有哪些品牌",
        produces="facets",
        capability="catalog_facets",
    )
    items = [
        {"value": "Akko", "count": 96},
        {"value": "Keychron", "count": 96},
        {"value": "Razer", "count": 96},
        {"value": "Wooting", "count": 96},
    ]
    state = {
        "route_plan": _goal(task),
        "task_status": {"task_1": {"status": "succeeded"}},
        "task_artifacts": {
            "task_1": _artifact(
                "task_1",
                "facets-1",
                "catalog_facets",
                "facets",
                usable=True,
                value={"facet": "brand", "items": items},
                reason="facets_found",
            )
        },
        "subquery_ledger": [
            _ledger("task_1", "facets-1", "catalog_facets", "usable", usable=True)
        ],
    }

    context = build_answer_context(state)
    answer_task = context["tasks"][0]

    assert context["rewritten_query"] == "处理用户请求"
    assert context["aggregation_contract"]["coverage_target"] == "处理用户请求"
    assert "业务事实来源" in context["aggregation_contract"]["forbidden"][0]
    assert context["completion"] == "full"
    assert answer_task["semantic_outcome"] == "answered_with_facts"
    assert answer_task["response_contract"]["must_include_values"] == [
        "Akko",
        "Keychron",
        "Razer",
        "Wooting",
    ]
    assert "只汇总 count 而省略 value" in answer_task["response_contract"]["forbidden"]


def test_spu_comparison_answer_contract_uses_series_as_primary_evidence() -> None:
    task = _task(
        "task_1",
        "对比两个鼠标系列",
        produces="comparison",
        capability="catalog_compare",
    )
    state = {
        "route_plan": _goal(task),
        "task_status": {"task_1": {"status": "succeeded"}},
        "task_artifacts": {
            "task_1": _artifact(
                "task_1",
                "compare-1",
                "catalog_compare",
                "comparison",
                usable=True,
                value={
                    "comparison_level": "spu",
                    "products": [],
                    "series": [{"spu_id": 1}, {"spu_id": 2}],
                    "series_differences": [],
                },
                reason="comparison_has_two_or_more_series",
            )
        },
        "subquery_ledger": [
            _ledger("task_1", "compare-1", "catalog_compare", "usable", usable=True)
        ],
    }

    context = build_answer_context(state)
    contract = context["tasks"][0]["response_contract"]

    assert "区分全系列共同规格与仅部分 SKU 提供的可选规格" in contract["required"]
    assert "用单个代表 SKU 的规格概括整个系列" in contract["forbidden"]
    assert "把不同可选规格自由组合成不存在的 SKU" in contract["forbidden"]


def test_no_match_is_a_fully_answered_negative_result() -> None:
    task = _task(
        "task_1",
        "查询十元以内的 4K 显示器",
        produces="products",
        capability="catalog_search",
    )
    state = {
        "route_plan": _goal(task),
        "task_status": {
            "task_1": {"status": "unavailable", "reason": "tool_outcome:empty"}
        },
        "task_artifacts": {
            "task_1": _artifact(
                "task_1",
                "search-1",
                "catalog_search",
                "products",
                usable=False,
                value=None,
                reason="no_matching_products",
            )
        },
        "subquery_ledger": [
            _ledger("task_1", "search-1", "catalog_search", "empty", usable=False)
        ],
    }

    context = build_answer_context(state)
    decision = OrchestratorDecision(
        type="grounded_response",
        response="当前没有找到十元以内的 4K 显示器。",
        control_action="finish_answer",
        used_tool_call_ids=["search-1"],
    )

    assert context["completion"] == "full"
    assert context["tasks"][0]["semantic_outcome"] == "answered_no_match"
    assert context["recommended_control_action"] == "finish_answer"
    assert validate_terminal_decision(
        decision,
        state["subquery_ledger"],
        planned_subquery_ids=["task_1"],
        resolved_task_ids=context["resolved_task_ids"],
        answerable_tool_call_ids=context["answerable_source_tool_call_ids"],
    ).valid


def test_answer_context_aggregates_partial_results_per_task() -> None:
    catalog_task = _task(
        "task_1",
        "推荐无线鼠标",
        produces="products",
        capability="catalog_search",
    )
    trend_task = _task(
        "task_2",
        "分析过去一年的销量增长率",
        produces="products",
        capability="catalog_search",
    )
    state = {
        "route_plan": _goal(catalog_task, trend_task),
        "task_status": {
            "task_1": {"status": "succeeded"},
            "task_2": {"status": "unavailable", "reason": "tool_outcome:unsupported"},
        },
        "task_artifacts": {
            "task_1": _artifact(
                "task_1",
                "search-1",
                "catalog_search",
                "products",
                usable=True,
                value={"products": [{"title": "Mouse"}], "query_plan": {}},
                reason="products_found",
            ),
            "task_2": _artifact(
                "task_2",
                "search-2",
                "catalog_search",
                "products",
                usable=False,
                value=None,
                reason="query_not_supported_by_tool",
            ),
        },
        "subquery_ledger": [
            _ledger("task_1", "search-1", "catalog_search", "usable", usable=True),
            _ledger(
                "task_2",
                "search-2",
                "catalog_search",
                "unsupported",
                usable=False,
            ),
        ],
    }

    context = build_answer_context(state)

    assert context["completion"] == "partial"
    assert context["recommended_control_action"] == "finish_partial"
    assert context["answerable_source_tool_call_ids"] == ["search-1"]
    assert [task["semantic_outcome"] for task in context["tasks"]] == [
        "answered_with_facts",
        "unsupported_capability",
    ]
    assert context["unavailable_parts"] == ["分析过去一年的销量增长率"]


@pytest.mark.asyncio
async def test_late_handoff_confirmation_does_not_switch_boundary_or_frontend_mode() -> None:
    task = _task(
        "task_1",
        "看看这个问题能不能处理",
        produces="documents",
        capability="knowledge_search",
    )
    state = cast(
        AgentState,
        {
            "message": "看看这个问题能不能处理",
            "route_plan": _goal(task),
            "task_status": {
                "task_1": {
                    "status": "unavailable",
                    "reason": "tool_outcome:unsupported",
                }
            },
            "task_artifacts": {
                "task_1": _artifact(
                    "task_1",
                    "knowledge-1",
                    "knowledge_search",
                    "documents",
                    usable=False,
                    value=None,
                    reason="query_not_supported_by_tool",
                )
            },
            "subquery_ledger": [
                _ledger(
                    "task_1",
                    "knowledge-1",
                    "knowledge_search",
                    "unsupported",
                    usable=False,
                )
            ],
            "tool_results": [
                {
                    "tool_call_id": "knowledge-1",
                    "name": "knowledge_search",
                    "execution": {
                        "ok": True,
                        "output": {"result_type": "empty", "documents": []},
                    },
                }
            ],
            "decision": {
                "type": "unavailable_response",
                "response": "现有信息不足以判断你是否希望办理操作。",
                "control_action": "finish_unavailable",
                "unavailable_parts": ["看看这个问题能不能处理"],
                "offer_handoff_confirmation": True,
            },
            "boundary": {
                "classification": "in_scope_auto",
                "reason": "可自动回答",
                "display_message": "可自动回答",
            },
            "intent": "knowledge_search",
            "products": [],
            "evidence": [],
            "order": None,
        },
    )
    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))

    guarded = await runtime._terminal_guard(state)
    finalized = await runtime._finalize_response(guarded)

    assert finalized["terminal_guard_status"] == "accepted"
    assert finalized["boundary"]["classification"] == "in_scope_auto"
    assert finalized["answer"].endswith(LATE_HANDOFF_CONFIRMATION)
    assert finalized["suggested_actions"] == []
    assert finalized["decision"]["offer_handoff_confirmation"] is True

    detailed_unavailable = "当前知识库不支持查询这项信息，暂时无法给出可靠结论。"
    state["decision"] = {
        "type": "unavailable_response",
        "response": detailed_unavailable,
        "control_action": "finish_unavailable",
        "unavailable_parts": ["看看这个问题能不能处理"],
        "offer_handoff_confirmation": False,
    }
    guarded = await runtime._terminal_guard(state)
    assert guarded["decision"]["response"] == detailed_unavailable
    finalized = await runtime._finalize_response(guarded)

    assert finalized["terminal_guard_status"] == "accepted"
    assert finalized["answer"] != detailed_unavailable
    assert "知识库" in finalized["answer"]


def test_handoff_confirmation_is_rejected_for_a_full_answer() -> None:
    decision = OrchestratorDecision(
        type="grounded_response",
        response="已经完整回答。",
        control_action="finish_answer",
        used_tool_call_ids=["search-1"],
        offer_handoff_confirmation=True,
    )

    validation = validate_terminal_decision(
        decision,
        [
            _ledger(
                "task_1",
                "search-1",
                "catalog_search",
                "usable",
                usable=True,
            )
        ],
        planned_subquery_ids=["task_1"],
        resolved_task_ids=["task_1"],
        answerable_tool_call_ids=["search-1"],
        handoff_confirmation_allowed=True,
    )

    assert not validation.valid
    assert validation.reason == "handoff_confirmation_not_allowed_for_terminal_state"


def test_terminal_guard_validation_rejects_boundary_changes() -> None:
    decision = OrchestratorDecision(
        type="unavailable_response",
        response="当前无法回答。",
        control_action="finish_unavailable",
        unavailable_parts=["当前问题"],
    )

    validation = validate_terminal_decision(
        decision,
        [
            _ledger(
                "task_1",
                "search-1",
                "catalog_search",
                "unsupported",
                usable=False,
            )
        ],
        boundary_consistent=False,
    )

    assert not validation.valid
    assert validation.reason == "terminal_boundary_changed"
