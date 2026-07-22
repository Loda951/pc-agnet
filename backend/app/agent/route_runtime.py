"""Request Router normalization, deterministic guards, and route projections."""

import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.boundary import BOUNDARY_POLICY
from app.agent.decisions import PlannedToolCall
from app.agent.intent import classify_intent, extract_order_id
from app.agent.prompts import (
    REQUEST_ROUTER_SYSTEM_PROMPT,
    build_request_router_user_prompt,
)
from app.agent.responses import (
    _is_identity_or_capability_question,
    _is_safe_direct_request,
)
from app.agent.routing import (
    RequestRoutePlan,
    RoutedSubquery,
    blocked_subqueries,
    tool_planning_subqueries,
)
from app.agent.state import AgentState
from app.services.memory import MemoryService


def _contextual_intent(
    message: str,
    working_memory: dict[str, Any],
    memory_service: MemoryService,
) -> str:
    intent = memory_service.resolve_intent(
        message,
        classify_intent(message),
        working_memory,
    )
    if intent == "general" and _is_v2_product_followup(message, working_memory):
        return "product_recommendation"
    if intent == "general" and _is_v2_policy_followup(message, working_memory):
        return "after_sales"
    return intent


def _reuse_comparison_context(
    plan: RequestRoutePlan,
    working_memory: dict[str, Any],
) -> RequestRoutePlan:
    """Collapse redundant discovery tasks when Router targets an existing comparison pair."""
    sku_ids = _context_comparison_sku_ids(working_memory)
    if len(sku_ids) < 2:
        return plan

    tasks = {item.id: item for item in tool_planning_subqueries(plan)}
    if len(tasks) == 1:
        only_task = next(iter(tasks.values()))
        sources = {item.source for item in only_task.input_requirements}
        if only_task.capability == "catalog_compare" and sources == {"comparison_context"}:
            return plan

    for comparison_task in tasks.values():
        if comparison_task.capability != "catalog_compare" or not comparison_task.depends_on:
            continue
        dependency_ids = set(comparison_task.depends_on)
        if set(tasks) != dependency_ids | {comparison_task.id}:
            continue
        dependencies = [tasks[item_id] for item_id in comparison_task.depends_on]
        if not all(
            item.capability == "catalog_search" and item.result_selector is None
            for item in dependencies
        ):
            continue
        matched_ids = {
            sku_id
            for item in dependencies
            for sku_id in sku_ids
            if re.search(rf"(?<!\d){sku_id}(?!\d)", item.query)
        }
        if matched_ids != set(sku_ids):
            continue
        collapsed = RoutedSubquery.model_validate(
            {
                **comparison_task.model_dump(mode="python"),
                "depends_on": [],
                "input_requirements": [
                    {
                        "name": "comparison_products",
                        "source": "comparison_context",
                    }
                ],
                "reason_code": "reuse_confirmed_comparison_context",
            }
        )
        return RequestRoutePlan(
            rewritten_query=plan.rewritten_query,
            subqueries=[collapsed],
        )
    return plan


def _resolve_compare_sku_ids(message: str, working_memory: dict[str, Any]) -> list[int]:
    catalog = working_memory.get("catalog")
    if not isinstance(catalog, dict):
        return []
    candidates = catalog.get("candidate_sku_ids")
    if not isinstance(candidates, list):
        return []

    indexes: list[int] = []
    ordinal_markers = [
        (0, ("第一个", "第一款", "第1个", "第1款", "1号")),
        (1, ("第二个", "第二款", "第2个", "第2款", "2号")),
        (2, ("第三个", "第三款", "第3个", "第3款", "3号")),
        (3, ("第四个", "第四款", "第4个", "第4款", "4号")),
        (4, ("第五个", "第五款", "第5个", "第5款", "5号")),
        (5, ("第六个", "第六款", "第6个", "第6款", "6号")),
    ]
    for index, markers in ordinal_markers:
        if any(marker in message for marker in markers):
            indexes.append(index)
    is_compare = any(term in message for term in ("对比", "比较", "区别", "哪个好"))
    if not indexes and is_compare and any(
        term in message for term in ("这些", "这几个", "上面的")
    ):
        indexes = list(range(len(candidates)))
    if not indexes and any(term in message for term in ("这款", "这个", "刚才那个")):
        referenced_sku_id = catalog.get("referenced_sku_id")
        if isinstance(referenced_sku_id, int) and referenced_sku_id in candidates:
            indexes = [candidates.index(referenced_sku_id)]
        elif candidates:
            indexes = [0]

    resolved: list[int] = []
    for index in indexes:
        if index >= len(candidates):
            continue
        value = candidates[index]
        if isinstance(value, int) and value not in resolved:
            resolved.append(value)
    return resolved[:10]


def _is_v2_product_followup(message: str, working_memory: dict[str, Any]) -> bool:
    catalog = working_memory.get("catalog")
    if not isinstance(catalog, dict):
        return False
    has_catalog_context = bool(catalog.get("query_plan") or catalog.get("candidate_sku_ids"))
    return has_catalog_context and any(
        term in message
        for term in (
            "换成",
            "换个",
            "不要",
            "排除",
            "避开",
            "不考虑",
            "无线",
            "有线",
            "便宜",
            "贵一点",
            "这款",
            "这个",
            "第一个",
            "第二个",
            "第三个",
        )
    )


def _fallback_catalog_query(state: AgentState) -> str:
    """Build query-only context when the runtime has no orchestrator LLM."""
    message = state["message"].strip()
    working_memory = state.get("working_memory", {})
    explicit_categories = (
        "鼠标",
        "键盘",
        "耳机",
        "显示器",
        "摄像头",
        "音箱",
        "mouse",
        "keyboard",
        "headset",
        "monitor",
        "webcam",
        "speaker",
    )
    if (
        any(category in message.casefold() for category in explicit_categories)
        or not _is_v2_product_followup(message, working_memory)
    ):
        return message
    catalog = working_memory.get("catalog")
    query_plan = catalog.get("query_plan") if isinstance(catalog, dict) else None
    previous_query = query_plan.get("query") if isinstance(query_plan, dict) else None
    if not isinstance(previous_query, str) or not previous_query.strip():
        return message
    return f"此前商品需求：{previous_query.strip()}；当前补充要求：{message}"


def _is_v2_policy_followup(message: str, working_memory: dict[str, Any]) -> bool:
    policy = working_memory.get("policy")
    if not isinstance(policy, dict) or not policy.get("last_query"):
        return False
    return any(
        term in message
        for term in (
            "这个政策",
            "该政策",
            "这个规则",
            "这条规则",
            "那保修",
            "保修",
            "还有呢",
        )
    )


def _resolve_order_id(
    message: str,
    explicit_order_id: int | None,
    working_memory: dict[str, Any],
    memory_service: MemoryService,
) -> int | None:
    if explicit_order_id is not None:
        return explicit_order_id
    order = working_memory.get("order")
    if isinstance(order, dict) and any(
        term in message for term in ("这个订单", "这笔订单", "刚才的订单", "上一单", "这单")
    ):
        value = order.get("last_order_id")
        if isinstance(value, int):
            return value
    return memory_service.resolve_order_id(message, explicit_order_id, working_memory)


def _knowledge_memory_view(working_memory: dict[str, Any]) -> dict[str, Any]:
    policy = working_memory.get("policy")
    if not isinstance(policy, dict) or not policy.get("last_query"):
        return working_memory
    return {**working_memory, "last_policy_query": policy["last_query"]}


def _tool_result_payload(result: Any) -> dict[str, Any]:
    if result.ok and result.output is not None:
        return result.output
    return result.model_dump(mode="json", exclude={"output"})


def _request_router_messages(
    state: AgentState,
) -> list[SystemMessage | HumanMessage | AIMessage]:
    messages: list[SystemMessage | HumanMessage | AIMessage] = [
        SystemMessage(content=REQUEST_ROUTER_SYSTEM_PROMPT)
    ]
    for item in state.get("history", []):
        content = item.get("content", "")
        if not content:
            continue
        if item.get("role") == "user":
            messages.append(HumanMessage(content=content))
        elif item.get("role") == "assistant":
            messages.append(AIMessage(content=content))
    messages.append(
        HumanMessage(
            content=build_request_router_user_prompt(
                message=state["message"],
                working_memory=state.get("working_memory", {}),
                explicit_user_preferences=state.get("memory", []),
            )
        )
    )
    return messages


def _fallback_rewritten_query(
    state: AgentState,
    memory_service: MemoryService,
) -> str:
    message = " ".join(state["message"].split())
    working_memory = state.get("working_memory", {})
    intent = _contextual_intent(message, working_memory, memory_service)
    if intent == "product_recommendation":
        return _fallback_catalog_query({**state, "message": message})
    if intent == "after_sales":
        return memory_service.resolve_knowledge_query(
            message,
            _knowledge_memory_view(working_memory),
        )
    if intent == "order_status" and extract_order_id(message) is None:
        order_id = _resolve_order_id(
            message,
            None,
            working_memory,
            memory_service,
        )
        if order_id is not None:
            return f"{message}，订单号 {order_id}"
    return message


def _split_request_subqueries(rewritten_query: str) -> list[str]:
    segments = re.split(
        r"(?:，|,)?(?:另外|顺便|同时还|再帮我|并且帮我|also\s+help\s+me)",
        rewritten_query,
        flags=re.IGNORECASE,
    )
    cleaned = [" ".join(segment.strip(" ，,；;。").split()) for segment in segments]
    return [segment for segment in cleaned if segment] or [rewritten_query.strip()]


def _deterministic_pre_route_plan(message: str) -> RequestRoutePlan | None:
    """Return a terminal route plan only when every raw segment is unambiguous."""
    normalized = " ".join(message.split())
    if not normalized:
        return None

    subqueries: list[RoutedSubquery] = []
    for segment in _split_boundary_guard_segments(normalized):
        hard_boundary = _hard_route_boundary(segment)
        if hard_boundary is not None:
            disposition, reason_code = hard_boundary
        elif _is_high_confidence_direct_request(segment):
            disposition = "direct_response"
            reason_code = "runtime_direct_fast_path"
        elif subqueries and _is_terminal_boundary_continuation(segment):
            previous = subqueries[-1]
            if previous.disposition == "direct_response":
                return None
            subqueries[-1] = previous.model_copy(
                update={"query": f"{previous.query}，{segment}"}
            )
            continue
        else:
            # One executable or ambiguous segment means the Router must still rewrite and split
            # the complete request. This preserves mixed-intent and working-memory behavior.
            return None
        subqueries.append(
            RoutedSubquery(
                id=f"sq_{len(subqueries) + 1}",
                query=segment,
                disposition=disposition,
                reason_code=reason_code,
            )
        )
    return RequestRoutePlan(rewritten_query=normalized, subqueries=subqueries)


def _is_terminal_boundary_continuation(message: str) -> bool:
    compact = re.sub(r"\s+", "", message.casefold())
    if any(
        marker in compact
        for marker in (
            "推荐",
            "比较",
            "对比",
            "查询订单",
            "查订单",
            "物流",
            "退货政策",
            "发票政策",
        )
    ):
        return False
    return any(
        marker in compact
        for marker in ("对应", "这个", "这款", "它", "结果", "sku", "型号", "编号")
    )


def _is_high_confidence_direct_request(message: str) -> bool:
    compact = re.sub(r"[\s，。！？!?、,.]", "", message.casefold())
    if compact in {
        "你好",
        "您好",
        "hello",
        "hi",
        "谢谢",
        "谢谢你",
        "再见",
    }:
        return True
    identity = compact.removeprefix("请问")
    if identity in {
        "你是谁",
        "你是什么",
        "你能做什么",
        "你能帮我做什么",
        "你会什么",
        "怎么用你",
    }:
        return True
    return classify_intent(message) == "purchase_guidance"


def _fallback_route_disposition(
    query: str,
    working_memory: dict[str, Any],
) -> str:
    hard_boundary = BOUNDARY_POLICY.route_guard(query)
    if hard_boundary is not None:
        return hard_boundary[0]
    if _is_identity_or_capability_question(query) or _is_safe_direct_request(query):
        return "direct_response"
    if classify_intent(query) == "purchase_guidance":
        return "direct_response"
    compact = re.sub(r"\s+", "", query.casefold())
    if compact in {"这个呢", "这个怎么样", "帮我查一下", "看看这个", "那这个呢"}:
        has_context = any(
            bool(working_memory.get(key)) for key in ("catalog", "order", "policy")
        )
        if not has_context:
            return "clarification"
    return "tool_planning"


def _enforce_route_boundaries(
    plan: RequestRoutePlan,
    *,
    original_message: str = "",
) -> RequestRoutePlan:
    enforced: list[RoutedSubquery] = []
    for item in plan.subqueries:
        hard_boundary = _hard_route_boundary(item.query)
        disposition = hard_boundary[0] if hard_boundary else item.disposition
        reason_code = hard_boundary[1] if hard_boundary else item.reason_code
        task_updates: dict[str, Any] = {}
        if disposition != "tool_planning":
            task_updates = {
                "capability": None,
                "depends_on": [],
                "input_requirements": [],
                "produces": None,
                "result_selector": None,
            }
        enforced.append(
            item.model_copy(
                update={
                    "query": " ".join(item.query.split()),
                    "disposition": disposition,
                    "reason_code": reason_code,
                    **task_updates,
                }
            )
        )

    raw_segments = _split_boundary_guard_segments(original_message)
    raw_hard_boundaries = [
        (segment, hard_boundary)
        for segment in raw_segments
        if (hard_boundary := _hard_route_boundary(segment)) is not None
    ]
    if raw_hard_boundaries:
        has_safe_raw_segment = any(
            _hard_route_boundary(segment) is None for segment in raw_segments
        )
        if not has_safe_raw_segment:
            raw_dispositions = {
                hard_boundary[0] for _, hard_boundary in raw_hard_boundaries
            }
            if any(item.disposition not in raw_dispositions for item in enforced):
                segment, (disposition, reason_code) = raw_hard_boundaries[0]
                enforced = [
                    RoutedSubquery(
                        id="sq_1",
                        query=segment,
                        disposition=disposition,
                        reason_code=f"{reason_code}_original_request",
                    )
                ]
        else:
            existing_dispositions = {item.disposition for item in enforced}
            next_index = max(
                (int(item.id.removeprefix("sq_")) for item in enforced),
                default=0,
            )
            for segment, (disposition, reason_code) in raw_hard_boundaries:
                if disposition in existing_dispositions:
                    continue
                next_index += 1
                enforced.append(
                    RoutedSubquery(
                        id=f"sq_{next_index}",
                        query=segment,
                        disposition=disposition,
                        reason_code=f"{reason_code}_original_request",
                    )
                )
                existing_dispositions.add(disposition)
    while True:
        admitted_ids = {
            item.id for item in enforced if item.disposition == "tool_planning"
        }
        changed = False
        normalized: list[RoutedSubquery] = []
        for item in enforced:
            if item.disposition != "tool_planning" or set(item.depends_on) <= admitted_ids:
                normalized.append(item)
                continue
            changed = True
            normalized.append(
                item.model_copy(
                    update={
                        "disposition": "clarification",
                        "reason_code": "dependency_not_admitted",
                        "clarification_question": "请补充需要用于后续查询或比较的具体对象。",
                        "capability": None,
                        "depends_on": [],
                        "input_requirements": [],
                        "produces": None,
                        "result_selector": None,
                    }
                )
            )
        enforced = normalized
        if not changed:
            break
    return RequestRoutePlan(
        rewritten_query=" ".join(plan.rewritten_query.split()),
        subqueries=enforced,
    )


def _hard_route_boundary(query: str) -> tuple[str, str] | None:
    return BOUNDARY_POLICY.route_guard(query)


def _split_boundary_guard_segments(message: str) -> list[str]:
    normalized = " ".join(message.split())
    if not normalized:
        return []
    segments = re.split(
        r"[；;。！？!?]+|(?:，|,)(?=\s*(?:另外|顺便|同时|再|并|然后|告诉|帮我|"
        r"写|查|查询|推荐|取消|修改|申请|把))|(?:另外|顺便|同时还|再帮我|并且帮我)|"
        r"并(?=(?:告诉|帮我|写|查|查询|推荐|取消|修改|申请|把))",
        normalized,
        flags=re.IGNORECASE,
    )
    cleaned = [segment.strip(" ，,；;。") for segment in segments]
    return [segment for segment in cleaned if segment] or [normalized]


def _boundary_from_route_plan(plan: RequestRoutePlan) -> dict[str, Any]:
    tool_subqueries = tool_planning_subqueries(plan)
    blocked = blocked_subqueries(plan)
    if tool_subqueries:
        reason = (
            "请求包含可自动处理的只读子任务；其他子任务将按各自边界单独说明"
            if blocked
            else "所有子任务均已通过只读能力准入"
        )
        return BOUNDARY_POLICY.for_classification(
            "in_scope_auto", reason=reason
        ).model_dump(mode="json")
    dispositions = {item.disposition for item in plan.subqueries}
    if "security_refusal" in dispositions:
        classification = "security_refusal"
    elif "human_handoff" in dispositions:
        classification = "human_handoff_required"
    elif "unsupported" in dispositions:
        classification = "unsupported"
    elif "out_of_scope" in dispositions:
        classification = "out_of_scope"
    else:
        classification = "in_scope_auto"
    return BOUNDARY_POLICY.for_classification(classification).model_dump(mode="json")


def _intent_from_route_plan(plan: RequestRoutePlan) -> str:
    tool_subqueries = tool_planning_subqueries(plan)
    if tool_subqueries:
        return "request_router"
    dispositions = list(dict.fromkeys(item.disposition for item in plan.subqueries))
    return " + ".join(dispositions) or "clarification"


def _routed_query_for_call(state: AgentState, call: PlannedToolCall) -> str | None:
    for item in tool_planning_subqueries(state.get("route_plan")):
        if item.id == call.subquery.strip():
            return item.query
    return None


def _bound_compare_sku_ids(state: AgentState, call: PlannedToolCall) -> list[int]:
    """Resolve declared comparison inputs from context and upstream task artifacts."""
    task = next(
        (
            item
            for item in tool_planning_subqueries(state.get("route_plan"))
            if item.id == call.subquery.strip()
        ),
        None,
    )
    if task is None or not task.input_requirements:
        return []

    resolved: list[int] = []
    tasks = {
        item.id: item for item in tool_planning_subqueries(state.get("route_plan"))
    }
    for requirement in task.input_requirements:
        if requirement.source == "comparison_context":
            for sku_id in _context_comparison_sku_ids(state.get("working_memory", {})):
                if sku_id not in resolved:
                    resolved.append(sku_id)
            continue

        sku_id: int | None = None
        if requirement.source == "context_product":
            sku_id = _context_product_sku_id(state.get("working_memory", {}))
        elif requirement.task_id is not None:
            dependency = tasks.get(requirement.task_id)
            products = _task_result_products(state, requirement.task_id)
            sku_id = _select_task_product_sku_id(products, dependency)
        if sku_id is not None and sku_id not in resolved:
            resolved.append(sku_id)
    return resolved


def _context_comparison_sku_ids(working_memory: dict[str, Any]) -> list[int]:
    catalog = working_memory.get("catalog")
    if not isinstance(catalog, dict):
        return []
    comparison = catalog.get("comparison")
    values = comparison.get("sku_ids") if isinstance(comparison, dict) else None
    if not isinstance(values, list):
        return []
    return list(
        dict.fromkeys(
            item
            for item in values
            if isinstance(item, int) and not isinstance(item, bool) and item > 0
        )
    )[:10]


def _context_product_sku_id(working_memory: dict[str, Any]) -> int | None:
    catalog = working_memory.get("catalog")
    if not isinstance(catalog, dict):
        return None
    referenced = catalog.get("referenced_sku_id")
    if isinstance(referenced, int) and not isinstance(referenced, bool):
        return referenced
    candidates = catalog.get("candidate_sku_ids")
    if not isinstance(candidates, list):
        return None
    return next(
        (
            item
            for item in candidates
            if isinstance(item, int) and not isinstance(item, bool)
        ),
        None,
    )


def _task_result_products(state: AgentState, task_id: str) -> list[dict[str, Any]]:
    for wave in reversed(state.get("tool_waves", [])):
        calls = {
            str(item.get("id") or ""): item
            for item in wave.get("calls", [])
            if isinstance(item, dict)
        }
        for result in reversed(wave.get("results", [])):
            if not isinstance(result, dict):
                continue
            call_data = calls.get(str(result.get("tool_call_id") or ""), {})
            if str(call_data.get("subquery") or "").strip() != task_id:
                continue
            execution = result.get("execution")
            output = execution.get("output") if isinstance(execution, dict) else None
            products = output.get("products") if isinstance(output, dict) else None
            if isinstance(products, list):
                return [item for item in products if isinstance(item, dict)]
    return []


def _select_task_product_sku_id(
    products: list[dict[str, Any]],
    task: RoutedSubquery | None,
) -> int | None:
    if not products:
        return None
    selector = task.result_selector if task is not None else None
    if selector is None:
        return _product_sku_id(products[0])

    if selector.scope == "sku":
        ranked = sorted(
            products,
            key=lambda item: (
                -_non_negative_int(item.get("sku_sales_count")),
                _non_negative_int(item.get("sku_id")),
            ),
        )
    else:
        representatives: dict[int, dict[str, Any]] = {}
        for product in products:
            spu_id = _optional_positive_int(product.get("spu_id"))
            if spu_id is None:
                continue
            current = representatives.get(spu_id)
            if current is None or _representative_sku_key(product) < _representative_sku_key(
                current
            ):
                representatives[spu_id] = product
        ranked = sorted(
            representatives.values(),
            key=lambda item: (
                -_non_negative_int(item.get("sales_count")),
                _non_negative_int(item.get("spu_id")),
            ),
        )
    index = selector.rank - 1
    return _product_sku_id(ranked[index]) if index < len(ranked) else None


def _representative_sku_key(product: dict[str, Any]) -> tuple[int, int]:
    return (
        -_non_negative_int(product.get("sku_sales_count")),
        _non_negative_int(product.get("sku_id")),
    )


def _product_sku_id(product: dict[str, Any]) -> int | None:
    return _optional_positive_int(product.get("sku_id"))


def _optional_positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _non_negative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
