from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.agent.artifacts import (
    extract_wave_artifacts,
    initialize_task_runtime,
    refresh_task_status,
)
from app.agent.capabilities import decision_from_route_capabilities
from app.agent.decisions import OrchestratorDecision
from app.agent.graph import AgentRuntime, _followup_tool_call_allowed
from app.agent.outcomes import build_subquery_ledger, validate_terminal_decision
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
            "message": "那跟销量第二的比呢？",
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


def test_spu_compare_task_binds_series_ids_from_context_and_ranked_artifact() -> None:
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": "对比当前键盘系列和销量第二的键盘系列",
            "subqueries": [
                {
                    "id": "goal_1",
                    "query": "对比当前键盘系列和销量第二的键盘系列",
                    "disposition": "tool_planning",
                    "reason_code": "compare_ranked_series",
                    "tasks": [
                        {
                            "id": "task_1",
                            "goal_id": "goal_1",
                            "canonical_query": "查询键盘 SPU 销量第二的商品",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "ranked_product",
                            "answer_role": "internal",
                            "capability": "catalog_search",
                            "result_selector": {
                                "type": "sales_rank",
                                "rank": 2,
                                "scope": "spu",
                            },
                        },
                        {
                            "id": "task_2",
                            "goal_id": "goal_1",
                            "canonical_query": "比较两个键盘系列的全部在售版本",
                            "depends_on": ["task_1"],
                            "input_requirements": [
                                {"name": "left", "source": "context_product"},
                                {
                                    "name": "right",
                                    "source": "task_output",
                                    "task_id": "task_1",
                                },
                            ],
                            "produces": "comparison",
                            "answer_role": "user_facing",
                            "capability": "catalog_compare",
                            "comparison_level": "spu",
                        },
                    ],
                }
            ],
        }
    )
    state = cast(
        AgentState,
        {
            "message": "这个和销量第二的比",
            "route_plan": plan.model_dump(mode="json"),
            "working_memory_snapshot": {
                "catalog": {
                    "referenced_spu_id": 75,
                    "referenced_sku_id": 757,
                    "candidate_spu_ids": [75],
                    "candidate_sku_ids": [757],
                }
            },
            "tool_waves": [],
            "tool_results": [],
            "subquery_ledger": [],
            "tool_wave_count": 0,
            "orchestrator_call_count": 0,
        },
    )
    initialize_task_runtime(state)
    first_decision = decision_from_route_capabilities(plan, state)
    assert first_decision is not None
    assert [call.subquery for call in first_decision.tool_calls] == ["task_1"]

    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    first_call, _ = runtime._prepare_tool_call(state, first_decision.tool_calls[0])
    ranked_products = [
        _product(701, 70, sales_count=9000, sku_sales_count=900),
        _product(711, 71, sales_count=8000, sku_sales_count=600),
    ]
    first_wave = {
        "wave": 1,
        "calls": [first_call.model_dump(mode="json")],
        "results": [
            _successful_result(first_call.id, "catalog_search", ranked_products)
        ],
    }
    state["tool_waves"] = [first_wave]
    state["tool_results"] = list(first_wave["results"])
    state["tool_wave_count"] = 1
    extract_wave_artifacts(state)
    state["subquery_ledger"] = [
        item.model_dump(mode="json") for item in build_subquery_ledger([first_wave])
    ]
    refresh_task_status(state)

    assert state["task_artifacts"]["task_1"]["value"]["selected_spu_ids"] == [71]
    assert state["task_status"]["task_2"]["status"] == "ready"
    second_decision = decision_from_route_capabilities(plan, state)
    assert second_decision is not None
    compare_call, _ = runtime._prepare_tool_call(state, second_decision.tool_calls[0])
    assert compare_call.arguments["comparison_level"] == "spu"
    assert compare_call.arguments["spu_ids"] == [75, 71]
    assert "sku_ids" not in compare_call.arguments


def test_task_graph_rejects_cycles() -> None:
    payload = _plan().model_dump(mode="json")
    payload["subqueries"][0]["tasks"][0]["depends_on"] = ["sq_2"]

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


def test_next_wave_uses_ready_compare_task_without_reclassifying_raw_message() -> None:
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


def test_structured_compare_binding_does_not_append_raw_message_candidates() -> None:
    state = _state_after_first_wave()
    state["message"] = "那跟第二个比呢？"
    state["working_memory"]["catalog"]["candidate_sku_ids"] = [757, 999]
    decision = decision_from_route_capabilities(_plan(), state)
    assert decision is not None

    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    effective, _ = runtime._prepare_tool_call(state, decision.tool_calls[0])

    assert effective.arguments["sku_ids"] == [757, 711]


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


def test_two_goals_execute_as_root_wave_then_dependent_wave() -> None:
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": "对比当前商品和销量第二的键盘，再推荐一个鼠标",
            "subqueries": [
                {
                    "id": "goal_1",
                    "query": "对比当前商品和销量第二的键盘",
                    "disposition": "tool_planning",
                    "reason_code": "compare_with_ranked_keyboard",
                    "tasks": [
                        {
                            "id": "task_1",
                            "goal_id": "goal_1",
                            "canonical_query": "查询键盘 SPU 销量排行第二的商品",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "ranked_product",
                            "answer_role": "internal",
                            "capability": "catalog_search",
                            "result_selector": {
                                "type": "sales_rank",
                                "rank": 2,
                                "scope": "spu",
                            },
                        },
                        {
                            "id": "task_2",
                            "goal_id": "goal_1",
                            "canonical_query": "比较当前商品与键盘销量第二名的区别",
                            "depends_on": ["task_1"],
                            "input_requirements": [
                                {"name": "left", "source": "context_product"},
                                {
                                    "name": "right",
                                    "source": "task_output",
                                    "task_id": "task_1",
                                },
                            ],
                            "produces": "comparison",
                            "answer_role": "user_facing",
                            "capability": "catalog_compare",
                        },
                    ],
                },
                {
                    "id": "goal_2",
                    "query": "推荐一个鼠标",
                    "disposition": "tool_planning",
                    "reason_code": "recommend_mouse",
                    "tasks": [
                        {
                            "id": "task_3",
                            "goal_id": "goal_2",
                            "canonical_query": "推荐一个鼠标",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "products",
                            "answer_role": "user_facing",
                            "capability": "catalog_search",
                        }
                    ],
                },
            ],
        }
    )
    state = cast(
        AgentState,
        {
            "message": "对比这个和销量第二的键盘，再推荐一个鼠标",
            "route_plan": plan.model_dump(mode="json"),
            "working_memory_snapshot": {
                "catalog": {
                    "referenced_sku_id": 757,
                    "candidate_sku_ids": [757],
                }
            },
            "tool_waves": [],
            "tool_results": [],
            "subquery_ledger": [],
            "tool_wave_count": 0,
            "orchestrator_call_count": 0,
        },
    )
    initialize_task_runtime(state)

    first_decision = decision_from_route_capabilities(plan, state)
    assert first_decision is not None
    assert [call.subquery for call in first_decision.tool_calls] == ["task_1", "task_3"]
    assert state["task_status"]["task_2"]["status"] == "pending"

    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    first_calls = [
        runtime._prepare_tool_call(state, call)[0].model_dump(mode="json")
        for call in first_decision.tool_calls
    ]
    keyboard_products = [
        _product(701, 70, sales_count=9000, sku_sales_count=900),
        _product(711, 71, sales_count=8000, sku_sales_count=600),
    ]
    mouse_products = [_product(801, 80, sales_count=6000, sku_sales_count=600)]
    first_wave = {
        "wave": 1,
        "calls": first_calls,
        "results": [
            _successful_result(first_calls[0]["id"], "catalog_search", keyboard_products),
            _successful_result(first_calls[1]["id"], "catalog_search", mouse_products),
        ],
    }
    state["tool_waves"] = [first_wave]
    state["tool_results"] = list(first_wave["results"])
    state["tool_wave_count"] = 1
    extract_wave_artifacts(state)
    state["subquery_ledger"] = [
        item.model_dump(mode="json") for item in build_subquery_ledger([first_wave])
    ]
    refresh_task_status(state)

    assert state["task_artifacts"]["task_1"]["value"]["selected_sku_ids"] == [711]
    assert state["task_status"]["task_1"]["status"] == "succeeded"
    assert state["task_status"]["task_2"]["status"] == "ready"
    assert state["task_status"]["task_3"]["status"] == "succeeded"

    second_decision = decision_from_route_capabilities(plan, state)
    assert second_decision is not None
    assert [call.subquery for call in second_decision.tool_calls] == ["task_2"]
    validated = runtime._validate_decision_budget(state, second_decision, call_count=1)
    assert validated.type == "tool_calls"
    compare_call, _ = runtime._prepare_tool_call(state, validated.tool_calls[0])
    assert compare_call.arguments["sku_ids"] == [757, 711]
    assert compare_call.arguments["query"] == "比较当前商品与键盘销量第二名的区别"

    compare_result = {
        "tool_call_id": compare_call.id,
        "name": "catalog_compare",
        "execution": {
            "tool_name": "catalog_compare",
            "ok": True,
            "output": {
                "result_type": "comparison",
                "products": [
                    _product(757, 75, sales_count=7500, sku_sales_count=550),
                    keyboard_products[1],
                ],
                "comparison_fields": ["price", "sales_count"],
            },
            "error": None,
        },
    }
    second_wave = {
        "wave": 2,
        "calls": [compare_call.model_dump(mode="json")],
        "results": [compare_result],
    }
    state["tool_waves"].append(second_wave)
    state["tool_results"].append(compare_result)
    state["tool_wave_count"] = 2
    extract_wave_artifacts(state)
    state["subquery_ledger"] = [
        item.model_dump(mode="json")
        for item in build_subquery_ledger(state["tool_waves"])
    ]
    refresh_task_status(state)

    assert state["task_status"]["task_2"]["status"] == "succeeded"
    terminal = OrchestratorDecision(
        type="grounded_response",
        response="比较结果与鼠标推荐均已完成。",
        control_action="finish_answer",
        used_tool_call_ids=[compare_call.id, first_calls[1]["id"]],
    )
    validation = validate_terminal_decision(
        terminal,
        state["subquery_ledger"],
        planned_subquery_ids=["task_2", "task_3"],
        resolved_task_ids=["task_1", "task_2", "task_3"],
        usable_artifact_tool_call_ids=[
            artifact["source_tool_call_id"]
            for artifact in state["task_artifacts"].values()
            if artifact["usable"]
        ],
    )
    assert validation.valid is True


def _runtime_state(
    plan: RequestRoutePlan,
    message: str,
    *,
    referenced_sku_id: int | None = None,
) -> AgentState:
    catalog = (
        {
            "referenced_sku_id": referenced_sku_id,
            "candidate_sku_ids": [referenced_sku_id],
        }
        if referenced_sku_id is not None
        else {}
    )
    state = cast(
        AgentState,
        {
            "message": message,
            "route_plan": plan.model_dump(mode="json"),
            "working_memory_snapshot": {"catalog": catalog},
            "tool_waves": [],
            "tool_results": [],
            "subquery_ledger": [],
            "tool_wave_count": 0,
            "orchestrator_call_count": 0,
        },
    )
    initialize_task_runtime(state)
    return state


def _apply_wave(
    state: AgentState,
    calls: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    wave = {
        "wave": len(state["tool_waves"]) + 1,
        "calls": calls,
        "results": results,
    }
    state["tool_waves"].append(wave)
    state["tool_results"].extend(results)
    state["tool_wave_count"] = wave["wave"]
    extract_wave_artifacts(state)
    state["subquery_ledger"] = [
        item.model_dump(mode="json")
        for item in build_subquery_ledger(state["tool_waves"])
    ]
    refresh_task_status(state)


def test_scheduler_is_not_dependent_on_task_order_or_rank_two() -> None:
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": "对比当前显示器和销量第三的显示器，再推荐一个键盘",
            "subqueries": [
                {
                    "id": "goal_7",
                    "query": "对比当前显示器和销量第三的显示器",
                    "disposition": "tool_planning",
                    "reason_code": "compare_third_monitor",
                    "tasks": [
                        {
                            "id": "task_8",
                            "goal_id": "goal_7",
                            "canonical_query": "比较当前显示器与销量第三的显示器",
                            "depends_on": ["task_7"],
                            "input_requirements": [
                                {"name": "current", "source": "context_product"},
                                {
                                    "name": "ranked",
                                    "source": "task_output",
                                    "task_id": "task_7",
                                },
                            ],
                            "produces": "comparison",
                            "answer_role": "user_facing",
                            "capability": "catalog_compare",
                        },
                        {
                            "id": "task_7",
                            "goal_id": "goal_7",
                            "canonical_query": "查询显示器 SPU 销量排行第三的商品",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "ranked_product",
                            "answer_role": "internal",
                            "capability": "catalog_search",
                            "result_selector": {
                                "type": "sales_rank",
                                "rank": 3,
                                "scope": "spu",
                            },
                        },
                    ],
                },
                {
                    "id": "goal_8",
                    "query": "推荐一个键盘",
                    "disposition": "tool_planning",
                    "reason_code": "recommend_keyboard",
                    "tasks": [
                        {
                            "id": "task_9",
                            "goal_id": "goal_8",
                            "canonical_query": "推荐一个键盘",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "products",
                            "answer_role": "user_facing",
                            "capability": "catalog_search",
                        }
                    ],
                },
            ],
        }
    )
    state = _runtime_state(
        plan,
        "对比这个和销量第三的显示器，再推荐一个键盘",
        referenced_sku_id=990,
    )
    first = decision_from_route_capabilities(plan, state)
    assert first is not None
    assert [call.subquery for call in first.tool_calls] == ["task_7", "task_9"]

    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    calls = [
        runtime._prepare_tool_call(state, call)[0].model_dump(mode="json")
        for call in first.tool_calls
    ]
    ranked = [
        _product(101, 10, sales_count=9000, sku_sales_count=900),
        _product(201, 20, sales_count=8000, sku_sales_count=800),
        _product(301, 30, sales_count=7000, sku_sales_count=700),
    ]
    _apply_wave(
        state,
        calls,
        [
            _successful_result(calls[0]["id"], "catalog_search", ranked),
            _successful_result(
                calls[1]["id"],
                "catalog_search",
                [_product(401, 40, sales_count=6000, sku_sales_count=600)],
            ),
        ],
    )

    assert state["task_artifacts"]["task_7"]["value"]["selected_sku_ids"] == [301]
    assert state["task_status"]["task_8"]["status"] == "ready"
    second = decision_from_route_capabilities(plan, state)
    assert second is not None
    validated = runtime._validate_decision_budget(state, second, call_count=1)
    compare, _ = runtime._prepare_tool_call(state, validated.tool_calls[0])
    assert compare.subquery == "task_8"
    assert compare.arguments["sku_ids"] == [990, 301]


def test_sku_rank_and_policy_search_share_root_wave_without_cross_talk() -> None:
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": "对比当前键盘和 SKU 销量第二的版本，并查询退货政策",
            "subqueries": [
                {
                    "id": "goal_3",
                    "query": "对比当前键盘和 SKU 销量第二的版本",
                    "disposition": "tool_planning",
                    "reason_code": "compare_second_sku",
                    "tasks": [
                        {
                            "id": "task_4",
                            "goal_id": "goal_3",
                            "canonical_query": "查询键盘 SKU 销量排行第二的版本",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "ranked_product",
                            "answer_role": "internal",
                            "capability": "catalog_search",
                            "result_selector": {
                                "type": "sales_rank",
                                "rank": 2,
                                "scope": "sku",
                            },
                        },
                        {
                            "id": "task_5",
                            "goal_id": "goal_3",
                            "canonical_query": "比较当前键盘与 SKU 销量第二的版本",
                            "depends_on": ["task_4"],
                            "input_requirements": [
                                {"name": "current", "source": "context_product"},
                                {
                                    "name": "ranked",
                                    "source": "task_output",
                                    "task_id": "task_4",
                                },
                            ],
                            "produces": "comparison",
                            "answer_role": "user_facing",
                            "capability": "catalog_compare",
                        },
                    ],
                },
                {
                    "id": "goal_4",
                    "query": "查询退货政策",
                    "disposition": "tool_planning",
                    "reason_code": "return_policy",
                    "tasks": [
                        {
                            "id": "task_6",
                            "goal_id": "goal_4",
                            "canonical_query": "查询退货政策",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "documents",
                            "answer_role": "user_facing",
                            "capability": "policy_search",
                        }
                    ],
                },
            ],
        }
    )
    state = _runtime_state(
        plan,
        "对比这个和 SKU 销量第二的版本，并查询退货政策",
        referenced_sku_id=600,
    )
    first = decision_from_route_capabilities(plan, state)
    assert first is not None
    assert [(call.subquery, call.name) for call in first.tool_calls] == [
        ("task_4", "catalog_search"),
        ("task_6", "policy_search"),
    ]

    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    calls = [
        runtime._prepare_tool_call(state, call)[0].model_dump(mode="json")
        for call in first.tool_calls
    ]
    sku_ranked = [
        _product(501, 50, sales_count=9000, sku_sales_count=900),
        _product(502, 50, sales_count=9000, sku_sales_count=800),
        _product(503, 50, sales_count=9000, sku_sales_count=700),
    ]
    policy_result = {
        "tool_call_id": calls[1]["id"],
        "name": "policy_search",
        "execution": {
            "tool_name": "policy_search",
            "ok": True,
            "output": {
                "result_type": "documents",
                "documents": [
                    {
                        "source_type": "knowledge_document",
                        "source_id": 88,
                        "title": "退货政策",
                        "document_type": "policy",
                        "snippet": "符合条件的商品可在规定期限内申请退货。",
                        "score": 0.95,
                        "metadata": {},
                    }
                ],
                "search_strategy": "hybrid",
            },
            "error": None,
        },
    }
    _apply_wave(
        state,
        calls,
        [
            _successful_result(calls[0]["id"], "catalog_search", sku_ranked),
            policy_result,
        ],
    )

    assert state["task_artifacts"]["task_4"]["value"]["selected_sku_ids"] == [502]
    assert state["task_artifacts"]["task_6"]["evidence"][0] == {
        "source_tool_call_id": calls[1]["id"],
        "source_type": "knowledge_document",
        "source_id": 88,
        "title": "退货政策",
        "document_type": "policy",
    }
    assert state["task_status"]["task_5"]["status"] == "ready"
    second = decision_from_route_capabilities(plan, state)
    assert second is not None
    validated = runtime._validate_decision_budget(state, second, call_count=1)
    compare, _ = runtime._prepare_tool_call(state, validated.tool_calls[0])
    assert compare.arguments["sku_ids"] == [600, 502]


def test_order_artifact_binds_unique_candidate_while_catalog_goal_stays_independent() -> None:
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": "查询最近订单详情，再推荐一个鼠标",
            "subqueries": [
                {
                    "id": "goal_10",
                    "query": "查询最近订单详情",
                    "disposition": "tool_planning",
                    "reason_code": "latest_order_detail",
                    "tasks": [
                        {
                            "id": "task_10",
                            "goal_id": "goal_10",
                            "canonical_query": "查询最近订单候选",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "order",
                            "answer_role": "internal",
                            "capability": "order_lookup",
                        },
                        {
                            "id": "task_11",
                            "goal_id": "goal_10",
                            "canonical_query": "查询选中订单的详情",
                            "depends_on": ["task_10"],
                            "input_requirements": [
                                {
                                    "name": "order_id",
                                    "source": "task_output",
                                    "task_id": "task_10",
                                }
                            ],
                            "produces": "order",
                            "answer_role": "user_facing",
                            "capability": "order_lookup",
                        },
                    ],
                },
                {
                    "id": "goal_11",
                    "query": "推荐一个鼠标",
                    "disposition": "tool_planning",
                    "reason_code": "recommend_mouse",
                    "tasks": [
                        {
                            "id": "task_12",
                            "goal_id": "goal_11",
                            "canonical_query": "推荐一个鼠标",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "products",
                            "answer_role": "user_facing",
                            "capability": "catalog_search",
                        }
                    ],
                },
            ],
        }
    )
    state = _runtime_state(plan, "查询最近订单详情，再推荐一个鼠标")
    first = decision_from_route_capabilities(plan, state)
    assert first is not None
    assert [(call.subquery, call.name) for call in first.tool_calls] == [
        ("task_10", "order_lookup"),
        ("task_12", "catalog_search"),
    ]

    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    calls = [
        runtime._prepare_tool_call(state, call)[0].model_dump(mode="json")
        for call in first.tool_calls
    ]
    order_candidates = {
        "tool_call_id": calls[0]["id"],
        "name": "order_lookup",
        "execution": {
            "tool_name": "order_lookup",
            "ok": True,
            "output": {
                "result_type": "order_candidates",
                "order": None,
                "candidates": [
                    {
                        "id": 4321,
                        "status": 2,
                        "status_label": "已发货",
                        "pay_amount": "399.00",
                        "created_at": "2026-07-20T12:00:00",
                        "item_count": 1,
                        "first_item_name": "鼠标",
                        "logistic_no": "SF123",
                    }
                ],
            },
            "error": None,
        },
    }
    _apply_wave(
        state,
        calls,
        [
            order_candidates,
            _successful_result(
                calls[1]["id"],
                "catalog_search",
                [_product(901, 90, sales_count=5000, sku_sales_count=500)],
            ),
        ],
    )

    assert state["task_status"]["task_11"]["status"] == "ready"
    second = decision_from_route_capabilities(plan, state)
    assert second is not None
    validated = runtime._validate_decision_budget(state, second, call_count=1)
    detail_call, _ = runtime._prepare_tool_call(state, validated.tool_calls[0])
    assert detail_call.subquery == "task_11"
    assert detail_call.arguments["order_id"] == 4321
    assert detail_call.arguments["query"] == "查询选中订单的详情"
