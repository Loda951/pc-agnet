import json
import re
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import AgentRuntime
from app.core.config import Settings
from app.schemas.chat import ChatRequest
from app.schemas.context import MemoryChanges, PreparedTurn, WorkingMemoryV2
from app.tools.contracts import ToolContract
from app.tools.schemas import ToolExecutionResult


def _route_message(rewritten_query: str, goals: list[dict[str, Any]]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": "route-e2e",
                "name": "route_request",
                "args": {
                    "rewritten_query": rewritten_query,
                    "subqueries": goals,
                },
                "type": "tool_call",
            }
        ],
    )


class _RouterModel:
    def __init__(self, response: AIMessage):
        self.response = response
        self.call_count = 0

    def bind_tools(self, _: list[dict[str, Any]], **__: Any) -> "_RouterModel":
        return self

    async def ainvoke(self, _: list[Any]) -> AIMessage:
        self.call_count += 1
        return self.response


def _tagged_json(content: str, tag: str) -> Any:
    match = re.search(rf"<{tag}>\n(.*?)\n</{tag}>", content, re.DOTALL)
    if match is None:
        raise AssertionError(f"answer prompt is missing <{tag}>")
    return json.loads(match.group(1))


class _ArtifactAnswerModel:
    """A schema-constrained answer double that can only read the Artifact prompt."""

    def __init__(self):
        self.answer_call_count = 0
        self.planner_call_count = 0
        self.last_used_tool_call_ids: list[str] = []

    def bind_tools(self, tools: list[dict[str, Any]], **_: Any) -> "_BoundArtifactAnswerModel":
        names = {str(item["function"]["name"]) for item in tools}
        role = (
            "answer"
            if names
            & {
                "finish_answer",
                "finish_partial",
                "finish_unavailable",
                "ask_clarification",
            }
            else "planner"
        )
        return _BoundArtifactAnswerModel(self, role)

    def answer(self, messages: list[Any]) -> AIMessage:
        self.answer_call_count += 1
        content = str(messages[-1].content)
        answer_context = _tagged_json(content, "answer_context")
        parts: list[str] = []
        used_ids: list[str] = []

        for task in answer_context["tasks"]:
            artifact = task["artifact"]
            assert task["semantic_outcome"] == "answered_with_facts"
            used_ids.append(artifact["source_tool_call_id"])
            value = artifact["facts"]
            tool_name = artifact["source_tool_name"]
            if tool_name == "catalog_compare":
                products = value["products"]
                rendered = "；".join(
                    f"{item['title']}（SKU {item['sku_id']}，¥{item['price']}）"
                    for item in products
                )
                parts.append(f"对比结果：{rendered}")
            elif tool_name == "catalog_search":
                product = value["products"][0]
                parts.append(
                    f"推荐：{product['title']}（SKU {product['sku_id']}，¥{product['price']}）"
                )
            elif tool_name in {"policy_search", "knowledge_search"}:
                snippets = "；".join(item["snippet"] for item in value["documents"])
                parts.append(f"政策：{snippets}")
            elif tool_name == "order_lookup":
                order = value["order"]
                parts.append(
                    f"订单 {order['id']} 当前状态为{order['status_label']}，"
                    f"实付 ¥{order['pay_amount']}。"
                )
            elif tool_name == "catalog_facets":
                values = "、".join(item["value"] for item in value["items"])
                parts.append(f"可选{value['facet']}：{values}")
            else:  # pragma: no cover - protects the test double's closed schema
                raise AssertionError(f"unsupported answer artifact: {tool_name}")

        self.last_used_tool_call_ids = used_ids
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "finish-e2e",
                    "name": "finish_answer",
                    "args": {
                        "response": "\n".join(parts),
                        "used_tool_call_ids": used_ids,
                    },
                    "type": "tool_call",
                }
            ],
        )

    def plan(self, messages: list[Any]) -> AIMessage:
        self.planner_call_count += 1
        content = str(messages[-1].content)
        tasks = _tagged_json(content, "routed_subqueries")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": f"planner_{task['id']}_{task['capability']}",
                    "name": task["capability"],
                    "args": {"subquery": task["id"]},
                    "type": "tool_call",
                }
                for task in tasks
            ],
        )


class _BoundArtifactAnswerModel:
    def __init__(self, owner: _ArtifactAnswerModel, role: str):
        self.owner = owner
        self.role = role

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        if self.role == "planner":
            return self.owner.plan(messages)
        return self.owner.answer(messages)


class _ContextService:
    def __init__(self, message: str, referenced_sku_id: int | None = None):
        working_memory = WorkingMemoryV2.model_validate(
            {
                "catalog": {
                    "referenced_sku_id": referenced_sku_id,
                    "candidate_sku_ids": (
                        [referenced_sku_id] if referenced_sku_id is not None else []
                    ),
                }
            }
        )
        self.prepared = PreparedTurn(
            user_id=7,
            conversation_id=41,
            user_message_id=51,
            run_id=61,
            message=message,
            working_memory=working_memory,
        )
        self.outcomes: list[dict[str, Any]] = []

    async def prepare_turn(self, _: int, __: int | None, ___: str) -> PreparedTurn:
        return self.prepared

    async def complete_turn(self, _: PreparedTurn, outcome: dict[str, Any]) -> MemoryChanges:
        self.outcomes.append(outcome)
        return MemoryChanges(working_memory=self.prepared.working_memory)


def _product(
    sku_id: int,
    spu_id: int,
    title: str,
    price: str,
    *,
    category: str,
    sales_count: int,
    sku_sales_count: int,
) -> dict[str, Any]:
    return {
        "sku_id": sku_id,
        "spu_id": spu_id,
        "title": title,
        "brand": "E2E",
        "category": category,
        "price": price,
        "stock": 10,
        "sales_count": sales_count,
        "sku_sales_count": sku_sales_count,
        "specs": {},
        "image_url": None,
    }


_PRODUCTS = {
    101: _product(
        101,
        10,
        "Rank-1 Monitor",
        "2199.00",
        category="monitor",
        sales_count=9000,
        sku_sales_count=900,
    ),
    201: _product(
        201,
        20,
        "Rank-2 Monitor",
        "1899.00",
        category="monitor",
        sales_count=8000,
        sku_sales_count=800,
    ),
    301: _product(
        301,
        30,
        "Rank-3 Monitor",
        "1599.00",
        category="monitor",
        sales_count=7000,
        sku_sales_count=700,
    ),
    401: _product(
        401,
        40,
        "Keyboard Choice",
        "499.00",
        category="keyboard",
        sales_count=6000,
        sku_sales_count=600,
    ),
    501: _product(
        501,
        50,
        "Rank-1 Keyboard SKU",
        "799.00",
        category="keyboard",
        sales_count=9000,
        sku_sales_count=900,
    ),
    502: _product(
        502,
        50,
        "Rank-2 Keyboard SKU",
        "699.00",
        category="keyboard",
        sales_count=9000,
        sku_sales_count=800,
    ),
    503: _product(
        503,
        50,
        "Rank-3 Keyboard SKU",
        "599.00",
        category="keyboard",
        sales_count=9000,
        sku_sales_count=700,
    ),
    600: _product(
        600,
        60,
        "Current Keyboard",
        "899.00",
        category="keyboard",
        sales_count=5000,
        sku_sales_count=500,
    ),
    700: _product(
        700,
        70,
        "Current Mouse",
        "399.00",
        category="mouse",
        sales_count=4000,
        sku_sales_count=400,
    ),
    710: _product(
        710,
        71,
        "Rank-1 Mouse",
        "329.00",
        category="mouse",
        sales_count=8500,
        sku_sales_count=850,
    ),
    720: _product(
        720,
        72,
        "Rank-2 Mouse",
        "279.00",
        category="mouse",
        sales_count=7500,
        sku_sales_count=750,
    ),
    800: _product(
        800,
        80,
        "Current Headset",
        "699.00",
        category="headset",
        sales_count=4000,
        sku_sales_count=400,
    ),
    801: _product(
        801,
        81,
        "Rank-1 Headset",
        "799.00",
        category="headset",
        sales_count=8800,
        sku_sales_count=880,
    ),
    802: _product(
        802,
        82,
        "Rank-2 Headset",
        "599.00",
        category="headset",
        sales_count=7800,
        sku_sales_count=780,
    ),
    901: _product(
        901, 90, "Mouse Choice", "299.00", category="mouse", sales_count=5000, sku_sales_count=500
    ),
    990: _product(
        990,
        99,
        "Current Monitor",
        "1999.00",
        category="monitor",
        sales_count=4000,
        sku_sales_count=400,
    ),
}


class _ScenarioToolExecutor:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(
        self,
        contract: ToolContract,
        arguments: dict[str, Any],
        _: dict[str, Any],
    ) -> ToolExecutionResult:
        name = contract.name
        self.calls.append((name, dict(arguments)))
        query = str(arguments.get("query") or "")

        if name == "catalog_search":
            if "显示器 SPU 销量排行第三" in query:
                products = [_PRODUCTS[101], _PRODUCTS[201], _PRODUCTS[301]]
            elif "推荐一个键盘" in query:
                products = [_PRODUCTS[401]]
            elif "键盘 SKU 销量排行第二" in query:
                products = [_PRODUCTS[501], _PRODUCTS[502], _PRODUCTS[503]]
            elif "键盘 SPU 销量排行第一" in query:
                products = [_PRODUCTS[501]]
            elif "鼠标 SPU 销量排行第一" in query:
                products = [_PRODUCTS[710], _PRODUCTS[720]]
            elif "鼠标 SPU 销量排行第二" in query:
                products = [_PRODUCTS[710], _PRODUCTS[720]]
            elif "耳机 SPU 销量排行第二" in query:
                products = [_PRODUCTS[801], _PRODUCTS[802]]
            elif "推荐一个无线鼠标" in query:
                products = [_PRODUCTS[901]]
            elif "推荐一个显示器" in query:
                products = [_PRODUCTS[301]]
            elif "推荐一个鼠标" in query:
                products = [_PRODUCTS[901]]
            else:  # pragma: no cover - catches accidental query rewriting
                raise AssertionError(f"unexpected frozen catalog query: {query}")
            output = {
                "result_type": "products",
                "products": products,
                "ranking_strategy": "fixture",
                "query_plan": {"query": query},
                "diagnostics": [],
            }
        elif name == "catalog_compare":
            sku_ids = arguments["sku_ids"]
            output = {
                "result_type": "comparison",
                "products": [_PRODUCTS[sku_id] for sku_id in sku_ids],
                "comparison_fields": ["price", "sales_count"],
                "missing_fields": {},
                "query_plan": {"query": query},
                "diagnostics": [],
            }
        elif name == "policy_search":
            output = {
                "result_type": "documents",
                "documents": [
                    {
                        "source_type": "knowledge_document",
                        "source_id": 88,
                        "title": "退货政策",
                        "document_type": "policy",
                        "snippet": "符合条件的商品可在七天内申请退货。",
                        "score": 0.95,
                        "metadata": {},
                    }
                ],
                "search_strategy": "hybrid",
            }
        elif name == "knowledge_search":
            output = {
                "result_type": "documents",
                "documents": [
                    {
                        "source_type": "knowledge_document",
                        "source_id": 89,
                        "title": "外设选购知识",
                        "document_type": "knowledge",
                        "snippet": "选购时应结合连接方式、尺寸和使用场景。",
                        "score": 0.91,
                        "metadata": {},
                    }
                ],
                "search_strategy": "hybrid",
            }
        elif name == "catalog_facets":
            output = {
                "result_type": "facets",
                "facet": "brand",
                "items": [
                    {"value": "E2E Display", "count": 3},
                    {"value": "Fixture Vision", "count": 2},
                ],
                "query_plan": {"query": query},
                "diagnostics": [],
            }
        elif name == "order_lookup" and arguments.get("order_id") is None:
            output = {
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
            }
        elif name == "order_lookup" and arguments.get("order_id") == 4321:
            output = {
                "result_type": "single_order",
                "order": {
                    "id": 4321,
                    "status": 2,
                    "status_label": "已发货",
                    "pay_amount": "399.00",
                    "created_at": "2026-07-20T12:00:00",
                    "items": [
                        {
                            "id": 1,
                            "sku_id": 901,
                            "sku_name": "Mouse Choice",
                            "sku_specs": {},
                            "price": "399.00",
                            "quantity": 1,
                        }
                    ],
                    "logistics": {
                        "express_company": "顺丰",
                        "logistic_no": "SF123",
                        "status": 2,
                        "trace": [],
                    },
                },
                "candidates": [],
            }
        else:  # pragma: no cover - closes the fake tool surface
            raise AssertionError(f"unexpected tool call: {name} {arguments}")

        return ToolExecutionResult(
            tool_name=contract.registry_name,
            ok=True,
            output=output,
        )


class _AuditRepository:
    def __init__(self, _: AsyncSession):
        pass

    async def add_tool_call(self, *_: Any) -> None:
        pass


def _runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    message: str,
    rewritten_query: str,
    goals: list[dict[str, Any]],
    referenced_sku_id: int | None = None,
) -> tuple[
    AgentRuntime,
    _RouterModel,
    _ArtifactAnswerModel,
    _ScenarioToolExecutor,
    _ContextService,
]:
    monkeypatch.setattr("app.agent.graph.ConversationRepository", _AuditRepository)
    router = _RouterModel(_route_message(rewritten_query, goals))
    answer = _ArtifactAnswerModel()
    executor = _ScenarioToolExecutor()
    context = _ContextService(message, referenced_sku_id)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        chat_model=answer,
        router_model=router,
        context_service=context,
        tool_executor=executor,
    )
    return runtime, router, answer, executor, context


@pytest.mark.asyncio
async def test_e2e_out_of_order_spu_rank_compare_and_independent_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "对比这个和销量第三的显示器，再推荐一个键盘"
    goals = [
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
                        {"name": "ranked", "source": "task_output", "task_id": "task_7"},
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
                    "result_selector": {"type": "sales_rank", "rank": 3, "scope": "spu"},
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
    ]
    runtime, router, answer, executor, context = _runtime(
        monkeypatch,
        message=message,
        rewritten_query="对比当前显示器和销量第三的显示器，再推荐一个键盘",
        goals=goals,
        referenced_sku_id=990,
    )

    response = await runtime.run(ChatRequest(message=message), user_id=7)
    state = context.outcomes[0]

    assert [[call["subquery"] for call in wave["calls"]] for wave in state["tool_waves"]] == [
        ["task_7", "task_9"],
        ["task_8"],
    ]
    assert executor.calls[-1][1]["sku_ids"] == [990, 301]
    assert "Rank-3 Monitor（SKU 301，¥1599.00）" in response.answer
    assert "Keyboard Choice（SKU 401，¥499.00）" in response.answer
    assert answer.last_used_tool_call_ids == [
        "router_task_8_catalog_compare",
        "router_task_9_catalog_search",
    ]
    _assert_successful_e2e(state, router, answer, context)


@pytest.mark.asyncio
async def test_e2e_sku_rank_compare_and_policy_share_first_wave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "对比这个和 SKU 销量第二的版本，并查询退货政策"
    goals = [
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
                    "result_selector": {"type": "sales_rank", "rank": 2, "scope": "sku"},
                },
                {
                    "id": "task_5",
                    "goal_id": "goal_3",
                    "canonical_query": "比较当前键盘与 SKU 销量第二的版本",
                    "depends_on": ["task_4"],
                    "input_requirements": [
                        {"name": "current", "source": "context_product"},
                        {"name": "ranked", "source": "task_output", "task_id": "task_4"},
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
    ]
    runtime, router, answer, executor, context = _runtime(
        monkeypatch,
        message=message,
        rewritten_query="对比当前键盘和 SKU 销量第二的版本，并查询退货政策",
        goals=goals,
        referenced_sku_id=600,
    )

    response = await runtime.run(ChatRequest(message=message), user_id=7)
    state = context.outcomes[0]

    assert [[call["subquery"] for call in wave["calls"]] for wave in state["tool_waves"]] == [
        ["task_4", "task_6"],
        ["task_5"],
    ]
    assert executor.calls[-1][1]["sku_ids"] == [600, 502]
    assert "Rank-2 Keyboard SKU（SKU 502，¥699.00）" in response.answer
    assert "符合条件的商品可在七天内申请退货" in response.answer
    assert response.evidence[0].source_id == 88
    assert state["task_artifacts"]["task_6"]["source_tool_call_id"] in (
        answer.last_used_tool_call_ids
    )
    _assert_successful_e2e(state, router, answer, context)


@pytest.mark.asyncio
async def test_e2e_order_candidate_binds_detail_and_catalog_stays_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "查询最近订单详情，再推荐一个鼠标"
    goals = [
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
                        {"name": "order_id", "source": "task_output", "task_id": "task_10"}
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
    ]
    runtime, router, answer, executor, context = _runtime(
        monkeypatch,
        message=message,
        rewritten_query=message,
        goals=goals,
    )

    response = await runtime.run(ChatRequest(message=message), user_id=7)
    state = context.outcomes[0]

    assert [[call["subquery"] for call in wave["calls"]] for wave in state["tool_waves"]] == [
        ["task_10", "task_12"],
        ["task_11"],
    ]
    assert executor.calls[-1][1]["order_id"] == 4321
    assert "订单 4321 当前状态为已发货" in response.answer
    assert "Mouse Choice（SKU 901，¥299.00）" in response.answer
    assert response.order is not None
    assert response.order.id == 4321
    assert "router_task_10_order_lookup" not in answer.last_used_tool_call_ids
    _assert_successful_e2e(state, router, answer, context)


def _assert_successful_e2e(
    state: dict[str, Any],
    router: _RouterModel,
    answer: _ArtifactAnswerModel,
    context: _ContextService,
    *,
    expected_planner_calls: int = 0,
    expected_tool_waves: int = 2,
) -> None:
    assert router.call_count == 1
    assert answer.planner_call_count == expected_planner_calls
    assert answer.answer_call_count == 1
    assert len(context.outcomes) == 1
    assert state["tool_wave_count"] == expected_tool_waves
    assert state["terminal_guard_status"] == "accepted"
    assert all(item["status"] == "succeeded" for item in state["task_status"].values())
    assert state["boundary"]["classification"] == "in_scope_auto"


def _unsupported_goal(goal_id: str, query: str, reason_code: str) -> dict[str, Any]:
    return {
        "id": goal_id,
        "query": query,
        "disposition": "unsupported",
        "reason_code": reason_code,
    }


def _spu_rank_compare_goal() -> dict[str, Any]:
    return {
        "id": "goal_20",
        "query": "对比当前显示器和销量第三的显示器",
        "disposition": "tool_planning",
        "reason_code": "compare_third_monitor",
        "tasks": [
            {
                "id": "task_20",
                "goal_id": "goal_20",
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
            {
                "id": "task_21",
                "goal_id": "goal_20",
                "canonical_query": "比较当前显示器与销量第三的显示器",
                "depends_on": ["task_20"],
                "input_requirements": [
                    {"name": "current", "source": "context_product"},
                    {
                        "name": "ranked",
                        "source": "task_output",
                        "task_id": "task_20",
                    },
                ],
                "produces": "comparison",
                "answer_role": "user_facing",
                "capability": "catalog_compare",
            },
        ],
    }


def _sku_rank_compare_goal() -> dict[str, Any]:
    return {
        "id": "goal_22",
        "query": "对比当前键盘和 SKU 销量第二的版本",
        "disposition": "tool_planning",
        "reason_code": "compare_second_sku",
        "tasks": [
            {
                "id": "task_22",
                "goal_id": "goal_22",
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
                "id": "task_23",
                "goal_id": "goal_22",
                "canonical_query": "比较当前键盘与 SKU 销量第二的版本",
                "depends_on": ["task_22"],
                "input_requirements": [
                    {"name": "current", "source": "context_product"},
                    {
                        "name": "ranked",
                        "source": "task_output",
                        "task_id": "task_22",
                    },
                ],
                "produces": "comparison",
                "answer_role": "user_facing",
                "capability": "catalog_compare",
            },
        ],
    }


def _order_detail_goal() -> dict[str, Any]:
    return {
        "id": "goal_24",
        "query": "查询最近订单详情",
        "disposition": "tool_planning",
        "reason_code": "latest_order_detail",
        "tasks": [
            {
                "id": "task_24",
                "goal_id": "goal_24",
                "canonical_query": "查询最近订单候选",
                "depends_on": [],
                "input_requirements": [],
                "produces": "order",
                "answer_role": "internal",
                "capability": "order_lookup",
            },
            {
                "id": "task_25",
                "goal_id": "goal_24",
                "canonical_query": "查询选中订单的详情",
                "depends_on": ["task_24"],
                "input_requirements": [
                    {
                        "name": "order_id",
                        "source": "task_output",
                        "task_id": "task_24",
                    }
                ],
                "produces": "order",
                "answer_role": "user_facing",
                "capability": "order_lookup",
            },
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "message",
        "admitted_goal",
        "unsupported_goal",
        "referenced_sku_id",
        "expected_waves",
        "expected_answer",
        "expected_bound_argument",
        "internal_call_id",
    ),
    [
        pytest.param(
            "对比这个和销量第三的显示器，另外帮我取消最近的订单",
            _spu_rank_compare_goal(),
            _unsupported_goal("goal_21", "帮我取消最近的订单", "cancel_order"),
            990,
            [["task_20"], ["task_21"]],
            "Rank-3 Monitor（SKU 301，¥1599.00）",
            ("sku_ids", [990, 301]),
            "router_task_20_catalog_search",
            id="spu-rank-compare-plus-cancel-order",
        ),
        pytest.param(
            "对比这个和 SKU 销量第二的版本，另外给这台键盘设置降价提醒",
            _sku_rank_compare_goal(),
            _unsupported_goal("goal_23", "给这台键盘设置降价提醒", "price_alert"),
            600,
            [["task_22"], ["task_23"]],
            "Rank-2 Keyboard SKU（SKU 502，¥699.00）",
            ("sku_ids", [600, 502]),
            "router_task_22_catalog_search",
            id="sku-rank-compare-plus-price-alert",
        ),
        pytest.param(
            "查询最近订单详情，另外扫一下商品条形码告诉我 SKU",
            _order_detail_goal(),
            _unsupported_goal("goal_25", "扫一下商品条形码告诉我 SKU", "barcode_scan"),
            None,
            [["task_24"], ["task_25"]],
            "订单 4321 当前状态为已发货",
            ("order_id", 4321),
            "router_task_24_order_lookup",
            id="order-detail-plus-barcode-scan",
        ),
    ],
)
async def test_e2e_chained_goal_completes_while_unsupported_goal_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    admitted_goal: dict[str, Any],
    unsupported_goal: dict[str, Any],
    referenced_sku_id: int | None,
    expected_waves: list[list[str]],
    expected_answer: str,
    expected_bound_argument: tuple[str, Any],
    internal_call_id: str,
) -> None:
    runtime, router, answer, executor, context = _runtime(
        monkeypatch,
        message=message,
        rewritten_query=message,
        goals=[admitted_goal, unsupported_goal],
        referenced_sku_id=referenced_sku_id,
    )

    response = await runtime.run(ChatRequest(message=message), user_id=7)
    state = context.outcomes[0]

    assert len(state["route_plan"]["subqueries"]) == 2
    assert len(state["blocked_subqueries"]) == 1
    assert state["blocked_subqueries"][0]["disposition"] == "unsupported"
    assert [
        [call["subquery"] for call in wave["calls"]] for wave in state["tool_waves"]
    ] == expected_waves
    assert len(executor.calls) == 2
    key, expected_value = expected_bound_argument
    assert executor.calls[-1][1][key] == expected_value
    assert expected_answer in response.answer
    assert "当前客服能力还不能可靠完成" in response.answer
    assert "另外：" in response.answer
    assert internal_call_id not in answer.last_used_tool_call_ids
    _assert_successful_e2e(state, router, answer, context)


def _rank_compare_goal(
    *,
    goal_number: int,
    rank_task_number: int,
    compare_task_number: int,
    category: str,
    scope: str,
    rank: int,
) -> dict[str, Any]:
    ordinal = {1: "一", 2: "二", 3: "三", 4: "四"}[rank]
    ranked_noun = "版本" if scope == "sku" else "商品"
    goal_id = f"goal_{goal_number}"
    rank_task_id = f"task_{rank_task_number}"
    compare_task_id = f"task_{compare_task_number}"
    return {
        "id": goal_id,
        "query": f"对比当前{category}和销量第{ordinal}的{category}",
        "disposition": "tool_planning",
        "reason_code": "compare_with_ranked_product",
        "tasks": [
            {
                "id": rank_task_id,
                "goal_id": goal_id,
                "canonical_query": (
                    f"查询{category} {scope.upper()} 销量排行第{ordinal}的{ranked_noun}"
                ),
                "depends_on": [],
                "input_requirements": [],
                "produces": "ranked_product",
                "answer_role": "internal",
                "capability": "catalog_search",
                "result_selector": {
                    "type": "sales_rank",
                    "rank": rank,
                    "scope": scope,
                },
            },
            {
                "id": compare_task_id,
                "goal_id": goal_id,
                "canonical_query": f"比较当前{category}与销量第{ordinal}的{category}",
                "depends_on": [rank_task_id],
                "input_requirements": [
                    {"name": "current", "source": "context_product"},
                    {
                        "name": "ranked",
                        "source": "task_output",
                        "task_id": rank_task_id,
                    },
                ],
                "produces": "comparison",
                "answer_role": "user_facing",
                "capability": "catalog_compare",
            },
        ],
    }


def _single_goal_multitask_cases() -> list[Any]:
    order_goal = {
        "id": "goal_103",
        "query": "查询最近订单的完整详情",
        "disposition": "tool_planning",
        "reason_code": "latest_order_detail",
        "tasks": [
            {
                "id": "task_106",
                "goal_id": "goal_103",
                "canonical_query": "查询最近订单候选",
                "depends_on": [],
                "input_requirements": [],
                "produces": "order",
                "answer_role": "internal",
                "capability": "order_lookup",
            },
            {
                "id": "task_107",
                "goal_id": "goal_103",
                "canonical_query": "查询选中订单的详情",
                "depends_on": ["task_106"],
                "input_requirements": [
                    {
                        "name": "order_id",
                        "source": "task_output",
                        "task_id": "task_106",
                    }
                ],
                "produces": "order",
                "answer_role": "user_facing",
                "capability": "order_lookup",
            },
        ],
    }
    cross_category_compare = {
        "id": "goal_104",
        "query": "比较销量第一的键盘和销量第一的鼠标",
        "disposition": "tool_planning",
        "reason_code": "compare_two_ranked_products",
        "tasks": [
            {
                "id": "task_108",
                "goal_id": "goal_104",
                "canonical_query": "查询键盘 SPU 销量排行第一的商品",
                "depends_on": [],
                "input_requirements": [],
                "produces": "ranked_product",
                "answer_role": "internal",
                "capability": "catalog_search",
                "result_selector": {"type": "sales_rank", "rank": 1, "scope": "spu"},
            },
            {
                "id": "task_109",
                "goal_id": "goal_104",
                "canonical_query": "查询鼠标 SPU 销量排行第一的商品",
                "depends_on": [],
                "input_requirements": [],
                "produces": "ranked_product",
                "answer_role": "internal",
                "capability": "catalog_search",
                "result_selector": {"type": "sales_rank", "rank": 1, "scope": "spu"},
            },
            {
                "id": "task_110",
                "goal_id": "goal_104",
                "canonical_query": "比较键盘销量第一名与鼠标销量第一名",
                "depends_on": ["task_108", "task_109"],
                "input_requirements": [
                    {
                        "name": "keyboard",
                        "source": "task_output",
                        "task_id": "task_108",
                    },
                    {
                        "name": "mouse",
                        "source": "task_output",
                        "task_id": "task_109",
                    },
                ],
                "produces": "comparison",
                "answer_role": "user_facing",
                "capability": "catalog_compare",
            },
        ],
    }
    recommendation_policy = {
        "id": "goal_105",
        "query": "推荐一个无线鼠标并说明是否支持七天退货",
        "disposition": "tool_planning",
        "reason_code": "recommend_with_return_policy",
        "tasks": [
            {
                "id": "task_111",
                "goal_id": "goal_105",
                "canonical_query": "推荐一个无线鼠标",
                "depends_on": [],
                "input_requirements": [],
                "produces": "products",
                "answer_role": "user_facing",
                "capability": "catalog_search",
            },
            {
                "id": "task_112",
                "goal_id": "goal_105",
                "canonical_query": "查询商城七天退货政策",
                "depends_on": [],
                "input_requirements": [],
                "produces": "documents",
                "answer_role": "user_facing",
                "capability": "policy_search",
            },
        ],
    }
    recommendation_knowledge = {
        "id": "goal_106",
        "query": "推荐一个键盘并说明选购机械轴时要注意什么",
        "disposition": "tool_planning",
        "reason_code": "recommend_with_buying_knowledge",
        "tasks": [
            {
                "id": "task_113",
                "goal_id": "goal_106",
                "canonical_query": "推荐一个键盘",
                "depends_on": [],
                "input_requirements": [],
                "produces": "products",
                "answer_role": "user_facing",
                "capability": "catalog_search",
            },
            {
                "id": "task_114",
                "goal_id": "goal_106",
                "canonical_query": "查询机械键盘轴体选购知识",
                "depends_on": [],
                "input_requirements": [],
                "produces": "documents",
                "answer_role": "user_facing",
                "capability": "knowledge_search",
            },
        ],
    }
    facets_recommendation = {
        "id": "goal_107",
        "query": "查看显示器可选品牌并推荐一个显示器",
        "disposition": "tool_planning",
        "reason_code": "inspect_brands_and_recommend",
        "tasks": [
            {
                "id": "task_115",
                "goal_id": "goal_107",
                "canonical_query": "查询显示器有哪些品牌",
                "depends_on": [],
                "input_requirements": [],
                "produces": "facets",
                "answer_role": "user_facing",
                "capability": "catalog_facets",
            },
            {
                "id": "task_116",
                "goal_id": "goal_107",
                "canonical_query": "推荐一个显示器",
                "depends_on": [],
                "input_requirements": [],
                "produces": "products",
                "answer_role": "user_facing",
                "capability": "catalog_search",
            },
        ],
    }
    order_policy = {
        "id": "goal_108",
        "query": "查询最近订单详情并说明这笔订单适用的退货政策",
        "disposition": "tool_planning",
        "reason_code": "order_detail_with_return_policy",
        "tasks": [
            {
                "id": "task_117",
                "goal_id": "goal_108",
                "canonical_query": "查询最近订单候选",
                "depends_on": [],
                "input_requirements": [],
                "produces": "order",
                "answer_role": "internal",
                "capability": "order_lookup",
            },
            {
                "id": "task_118",
                "goal_id": "goal_108",
                "canonical_query": "查询选中订单的详情",
                "depends_on": ["task_117"],
                "input_requirements": [
                    {
                        "name": "order_id",
                        "source": "task_output",
                        "task_id": "task_117",
                    }
                ],
                "produces": "order",
                "answer_role": "user_facing",
                "capability": "order_lookup",
            },
            {
                "id": "task_119",
                "goal_id": "goal_108",
                "canonical_query": "查询商城退货政策",
                "depends_on": [],
                "input_requirements": [],
                "produces": "documents",
                "answer_role": "user_facing",
                "capability": "policy_search",
            },
        ],
    }
    headset_compare_knowledge = {
        "id": "goal_109",
        "query": "对比当前耳机和销量第二的耳机并说明耳机选购要点",
        "disposition": "tool_planning",
        "reason_code": "compare_headset_with_knowledge",
        "tasks": [
            {
                "id": "task_120",
                "goal_id": "goal_109",
                "canonical_query": "查询耳机 SPU 销量排行第二的商品",
                "depends_on": [],
                "input_requirements": [],
                "produces": "ranked_product",
                "answer_role": "internal",
                "capability": "catalog_search",
                "result_selector": {"type": "sales_rank", "rank": 2, "scope": "spu"},
            },
            {
                "id": "task_121",
                "goal_id": "goal_109",
                "canonical_query": "比较当前耳机与销量第二的耳机",
                "depends_on": ["task_120"],
                "input_requirements": [
                    {"name": "current", "source": "context_product"},
                    {
                        "name": "ranked",
                        "source": "task_output",
                        "task_id": "task_120",
                    },
                ],
                "produces": "comparison",
                "answer_role": "user_facing",
                "capability": "catalog_compare",
            },
            {
                "id": "task_122",
                "goal_id": "goal_109",
                "canonical_query": "查询耳机选购知识",
                "depends_on": [],
                "input_requirements": [],
                "produces": "documents",
                "answer_role": "user_facing",
                "capability": "knowledge_search",
            },
        ],
    }
    return [
        pytest.param(
            "对比这个和销量第三的显示器",
            _rank_compare_goal(
                goal_number=100,
                rank_task_number=100,
                compare_task_number=101,
                category="显示器",
                scope="spu",
                rank=3,
            ),
            990,
            [[("task_100", "catalog_search")], [("task_101", "catalog_compare")]],
            ["Rank-3 Monitor（SKU 301，¥1599.00）"],
            0,
            id="current-monitor-vs-spu-rank-3",
        ),
        pytest.param(
            "对比这个和 SKU 销量第二的键盘版本",
            _rank_compare_goal(
                goal_number=101,
                rank_task_number=102,
                compare_task_number=103,
                category="键盘",
                scope="sku",
                rank=2,
            ),
            600,
            [[("task_102", "catalog_search")], [("task_103", "catalog_compare")]],
            ["Rank-2 Keyboard SKU（SKU 502，¥699.00）"],
            0,
            id="current-keyboard-vs-sku-rank-2",
        ),
        pytest.param(
            "对比这个和销量第一的鼠标",
            _rank_compare_goal(
                goal_number=102,
                rank_task_number=104,
                compare_task_number=105,
                category="鼠标",
                scope="spu",
                rank=1,
            ),
            700,
            [[("task_104", "catalog_search")], [("task_105", "catalog_compare")]],
            ["Rank-1 Mouse（SKU 710，¥329.00）"],
            0,
            id="current-mouse-vs-spu-rank-1",
        ),
        pytest.param(
            "查询最近订单的完整详情",
            order_goal,
            None,
            [[("task_106", "order_lookup")], [("task_107", "order_lookup")]],
            ["订单 4321 当前状态为已发货"],
            0,
            id="latest-order-candidate-then-detail",
        ),
        pytest.param(
            "比较销量第一的键盘和销量第一的鼠标",
            cross_category_compare,
            None,
            [
                [("task_108", "catalog_search"), ("task_109", "catalog_search")],
                [("task_110", "catalog_compare")],
            ],
            ["Rank-1 Keyboard SKU", "Rank-1 Mouse"],
            0,
            id="two-ranked-products-then-compare",
        ),
        pytest.param(
            "推荐一个无线鼠标并说明是否支持七天退货",
            recommendation_policy,
            None,
            [[("task_111", "catalog_search"), ("task_112", "policy_search")]],
            ["Mouse Choice（SKU 901，¥299.00）", "七天内申请退货"],
            0,
            id="recommendation-and-policy-in-parallel",
        ),
        pytest.param(
            "推荐一个键盘并说明选购机械轴时要注意什么",
            recommendation_knowledge,
            None,
            [[("task_113", "catalog_search"), ("task_114", "knowledge_search")]],
            ["Keyboard Choice（SKU 401，¥499.00）", "连接方式、尺寸和使用场景"],
            0,
            id="recommendation-and-knowledge-in-parallel",
        ),
        pytest.param(
            "查看显示器可选品牌并推荐一个显示器",
            facets_recommendation,
            None,
            [[("task_115", "catalog_facets"), ("task_116", "catalog_search")]],
            ["E2E Display、Fixture Vision", "Rank-3 Monitor（SKU 301，¥1599.00）"],
            0,
            id="facets-and-recommendation-in-parallel",
        ),
        pytest.param(
            "查询最近订单详情并说明这笔订单适用的退货政策",
            order_policy,
            None,
            [
                [("task_117", "order_lookup"), ("task_119", "policy_search")],
                [("task_118", "order_lookup")],
            ],
            ["订单 4321 当前状态为已发货", "七天内申请退货"],
            0,
            id="order-detail-chain-with-parallel-policy",
        ),
        pytest.param(
            "对比当前耳机和销量第二的耳机并说明耳机选购要点",
            headset_compare_knowledge,
            800,
            [
                [("task_120", "catalog_search"), ("task_122", "knowledge_search")],
                [("task_121", "catalog_compare")],
            ],
            ["Rank-2 Headset（SKU 802，¥599.00）", "连接方式、尺寸和使用场景"],
            0,
            id="ranked-headset-compare-with-parallel-knowledge",
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "message",
        "goal",
        "referenced_sku_id",
        "expected_waves",
        "expected_outputs",
        "expected_planner_calls",
    ),
    _single_goal_multitask_cases(),
)
async def test_e2e_single_goal_expands_to_multiple_tasks(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    goal: dict[str, Any],
    referenced_sku_id: int | None,
    expected_waves: list[list[tuple[str, str]]],
    expected_outputs: list[str],
    expected_planner_calls: int,
) -> None:
    runtime, router, answer, _, context = _runtime(
        monkeypatch,
        message=message,
        rewritten_query=message,
        goals=[goal],
        referenced_sku_id=referenced_sku_id,
    )

    response = await runtime.run(ChatRequest(message=message), user_id=7)
    state = context.outcomes[0]
    route_goals = state["route_plan"]["subqueries"]
    actual_waves = [
        [(call["subquery"], call["name"]) for call in wave["calls"]] for wave in state["tool_waves"]
    ]

    assert len(route_goals) == 1
    assert len(route_goals[0]["tasks"]) >= 2
    assert actual_waves == expected_waves
    assert {task["id"]: task["capability"] for task in route_goals[0]["tasks"]} == {
        task_id: tool_name for wave in expected_waves for task_id, tool_name in wave
    }
    for wave in state["tool_waves"]:
        for call in wave["calls"]:
            task = next(item for item in route_goals[0]["tasks"] if item["id"] == call["subquery"])
            assert call["canonical_query"] == task["canonical_query"]
            assert call["arguments"].get("query") == task["canonical_query"]
    for expected_output in expected_outputs:
        assert expected_output in response.answer
    _assert_successful_e2e(
        state,
        router,
        answer,
        context,
        expected_planner_calls=expected_planner_calls,
        expected_tool_waves=len(expected_waves),
    )
