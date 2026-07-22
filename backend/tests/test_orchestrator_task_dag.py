from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.agent.capabilities import decision_from_route_capabilities
from app.agent.graph import AgentRuntime, _followup_tool_call_allowed
from app.agent.outcomes import build_subquery_ledger
from app.agent.route_runtime import _reuse_comparison_context
from app.agent.routing import RequestRoutePlan
from app.agent.state import AgentState
from app.core.config import Settings


def _plan() -> RequestRoutePlan:
    return RequestRoutePlan.model_validate(
        {
            "rewritten_query": "对比当前商品和销量第二的键盘，再推荐一个鼠标",
            "subqueries": [
                {
                    "id": "sq_1",
                    "query": "查询键盘 SPU 销量排行第二的商品",
                    "disposition": "tool_planning",
                    "reason_code": "discover_ranked_keyboard",
                    "capability": "catalog_search",
                    "produces": "ranked_product",
                    "result_selector": {
                        "type": "sales_rank",
                        "rank": 2,
                        "scope": "spu",
                    },
                },
                {
                    "id": "sq_2",
                    "query": "比较当前商品与键盘销量第二名的区别",
                    "disposition": "tool_planning",
                    "reason_code": "compare_products",
                    "capability": "catalog_compare",
                    "depends_on": ["sq_1"],
                    "input_requirements": [
                        {"name": "left_product", "source": "context_product"},
                        {
                            "name": "right_product",
                            "source": "task_output",
                            "task_id": "sq_1",
                        },
                    ],
                    "produces": "comparison",
                },
                {
                    "id": "sq_3",
                    "query": "推荐一个鼠标",
                    "disposition": "tool_planning",
                    "reason_code": "recommend_mouse",
                    "capability": "catalog_search",
                    "produces": "products",
                },
            ],
        }
    )


def _product(
    sku_id: int,
    spu_id: int,
    *,
    sales_count: int,
    sku_sales_count: int,
) -> dict[str, Any]:
    return {
        "sku_id": sku_id,
        "spu_id": spu_id,
        "title": f"Keyboard {sku_id}",
        "brand": "Wooting",
        "category": "keyboard",
        "price": "399.00",
        "stock": 10,
        "sales_count": sales_count,
        "sku_sales_count": sku_sales_count,
        "specs": {},
    }


def _successful_result(
    call_id: str,
    name: str,
    products: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "tool_call_id": call_id,
        "name": name,
        "execution": {
            "tool_name": name,
            "ok": True,
            "output": {"result_type": "products", "products": products},
            "error": None,
        },
    }


def _state_after_first_wave() -> AgentState:
    plan = _plan()
    keyboard_products = [
        _product(701, 70, sales_count=9000, sku_sales_count=900),
        _product(702, 70, sales_count=9000, sku_sales_count=800),
        _product(703, 70, sales_count=9000, sku_sales_count=700),
        _product(711, 71, sales_count=8000, sku_sales_count=600),
        _product(712, 71, sales_count=8000, sku_sales_count=500),
        _product(721, 72, sales_count=7000, sku_sales_count=400),
    ]
    mouse_products = [_product(801, 80, sales_count=6000, sku_sales_count=600)]
    wave = {
        "wave": 1,
        "calls": [
            {
                "id": "router_sq_1_catalog_search",
                "name": "catalog_search",
                "arguments": {
                    "query": "查询键盘 SPU 销量排行第二的商品",
                    "limit": 2,
                },
                "subquery": "sq_1",
                "canonical_query": "查询键盘 SPU 销量排行第二的商品",
                "tool_query": "查询键盘 SPU 销量排行第二的商品",
            },
            {
                "id": "router_sq_3_catalog_search",
                "name": "catalog_search",
                "arguments": {"query": "推荐一个鼠标", "limit": 3},
                "subquery": "sq_3",
                "canonical_query": "推荐一个鼠标",
                "tool_query": "推荐一个鼠标",
            },
        ],
        "results": [
            _successful_result(
                "router_sq_1_catalog_search", "catalog_search", keyboard_products
            ),
            _successful_result(
                "router_sq_3_catalog_search", "catalog_search", mouse_products
            ),
        ],
    }
    return cast(
        AgentState,
        {
            "message": "对比这个和销量第二的键盘，再推荐一个鼠标",
            "route_plan": plan.model_dump(mode="json"),
            "working_memory": {
                "catalog": {
                    "referenced_sku_id": 757,
                    "candidate_sku_ids": [757],
                }
            },
            "tool_waves": [wave],
            "tool_results": wave["results"],
            "tool_wave_count": 1,
            "subquery_ledger": [
                entry.model_dump(mode="json") for entry in build_subquery_ledger([wave])
            ],
            "orchestrator_call_count": 0,
        },
    )


def test_task_graph_rejects_cycles() -> None:
    payload = _plan().model_dump(mode="json")
    payload["subqueries"][0]["depends_on"] = ["sq_2"]

    with pytest.raises(ValidationError, match="acyclic"):
        RequestRoutePlan.model_validate(payload)


def test_ready_scheduler_parallelizes_independent_root_tasks() -> None:
    decision = decision_from_route_capabilities(_plan(), {})

    assert decision is not None
    assert [call.subquery for call in decision.tool_calls] == ["sq_1", "sq_3"]
    assert decision.tool_calls[0].arguments == {"limit": 2}


def test_ranked_search_preparation_stays_within_public_tool_contract() -> None:
    plan = _plan()
    decision = decision_from_route_capabilities(plan, {})
    assert decision is not None

    state = cast(
        AgentState,
        {
            "message": "对比一下这个和销量第二的有什么区别",
            "route_plan": plan.model_dump(mode="json"),
            "working_memory": {
                "catalog": {
                    "referenced_sku_id": 757,
                    "candidate_sku_ids": [757],
                }
            },
        },
    )
    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))

    effective, _ = runtime._prepare_tool_call(state, decision.tool_calls[0])

    assert effective.arguments == {
        "query": "查询键盘 SPU 销量排行第二的商品",
        "limit": 2,
    }


def test_next_wave_binds_context_and_ranked_task_output_for_compare() -> None:
    state = _state_after_first_wave()
    decision = decision_from_route_capabilities(_plan(), state)

    assert decision is not None
    assert [call.subquery for call in decision.tool_calls] == ["sq_2"]

    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    validated = runtime._validate_decision_budget(state, decision, call_count=0)
    assert validated.type == "tool_calls"

    effective, _ = runtime._prepare_tool_call(state, validated.tool_calls[0])
    assert effective.arguments["sku_ids"] == [757, 711]
    assert effective.arguments["query"] == "比较当前商品与键盘销量第二名的区别"
    assert effective.canonical_query == "比较当前商品与键盘销量第二名的区别"
    assert _followup_tool_call_allowed(state, effective) is True


def test_comparison_followup_binds_previous_pair_without_catalog_search() -> None:
    working_memory = {
        "catalog": {
            "comparison": {"sku_ids": [757, 745]},
            "candidate_display": [
                {"sku_id": 757, "title": "Wooting 曜石 K08 黑色标准版"},
                {"sku_id": 745, "title": "Wooting 青锋 K07 黑色标准版"},
            ],
        }
    }
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": "比较已确认的两款键盘，判断哪个价格更低",
            "subqueries": [
                {
                    "id": "sq_1",
                    "query": "查询 Wooting 曜石 K08 黑色标准版（SKU ID: 757）的价格",
                    "disposition": "tool_planning",
                    "reason_code": "refresh_first_product",
                    "capability": "catalog_search",
                    "produces": "products",
                },
                {
                    "id": "sq_2",
                    "query": "查询 Wooting 青锋 K07 黑色标准版（SKU ID: 745）的价格",
                    "disposition": "tool_planning",
                    "reason_code": "refresh_second_product",
                    "capability": "catalog_search",
                    "produces": "products",
                },
                {
                    "id": "sq_3",
                    "query": "比较 Wooting 曜石 K08 和 Wooting 青锋 K07 的价格",
                    "disposition": "tool_planning",
                    "reason_code": "compare_prices",
                    "capability": "catalog_compare",
                    "depends_on": ["sq_1", "sq_2"],
                    "input_requirements": [
                        {"name": "first", "source": "task_output", "task_id": "sq_1"},
                        {"name": "second", "source": "task_output", "task_id": "sq_2"},
                    ],
                    "produces": "comparison",
                },
            ],
        }
    )
    plan = _reuse_comparison_context(plan, working_memory)

    assert len(plan.subqueries) == 1
    assert plan.subqueries[0].capability == "catalog_compare"
    assert plan.subqueries[0].input_requirements[0].source == "comparison_context"

    decision = decision_from_route_capabilities(plan, {})
    assert decision is not None
    assert [call.name for call in decision.tool_calls] == ["catalog_compare"]

    state = cast(
        AgentState,
        {
            "message": "哪个便宜",
            "route_plan": plan.model_dump(mode="json"),
            "working_memory": working_memory,
        },
    )
    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    effective, _ = runtime._prepare_tool_call(state, decision.tool_calls[0])

    assert effective.arguments["sku_ids"] == [757, 745]
    assert effective.arguments["limit"] == 5
