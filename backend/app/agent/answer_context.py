"""Task-centered input for the Answer Synthesizer.

The execution runtime keeps route, status, artifact, and ledger structures separate because
they serve scheduling and audit concerns.  The Answer Synthesizer should not have to join those
structures itself, so this module projects them into one user-facing record per routed task.
"""

from collections import Counter
from collections.abc import Mapping
from typing import Any, Literal

from app.agent.outcomes import is_active_ledger_entry
from app.agent.routing import user_facing_tasks

TaskSemanticOutcome = Literal[
    "answered_with_facts",
    "answered_no_match",
    "needs_clarification",
    "unsupported_capability",
    "temporarily_unavailable",
    "insufficient_evidence",
    "blocked_dependency",
    "incomplete",
]
TurnCompletion = Literal["full", "partial", "none"]

_RESOLVED_OUTCOMES = {"answered_with_facts", "answered_no_match"}


def build_answer_context(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the answer-only view of the current Task DAG execution."""
    rewritten_query = _route_rewritten_query(state)
    ledger_by_task = _latest_active_ledger_by_task(state)
    artifacts = _mapping(state.get("task_artifacts"))
    statuses = _mapping(state.get("task_status"))
    tasks: list[dict[str, Any]] = []

    for task in user_facing_tasks(state.get("route_plan")):
        status = _mapping(statuses.get(task.id))
        artifact = _mapping(artifacts.get(task.id))
        ledger_entry = ledger_by_task.get(task.id, {})
        semantic_outcome = _semantic_outcome(status, artifact, ledger_entry, state)
        source_tool_call_id = str(
            artifact.get("source_tool_call_id")
            or ledger_entry.get("tool_call_id")
            or ""
        )
        source_tool_name = str(
            artifact.get("source_tool_name")
            or ledger_entry.get("tool_name")
            or ""
        )
        facts = artifact.get("value") if artifact.get("usable") else None
        tasks.append(
            {
                "task_id": task.id,
                "goal_id": task.goal_id,
                "question": task.canonical_query,
                "status": str(status.get("status") or "pending"),
                "semantic_outcome": semantic_outcome,
                "resolved": semantic_outcome in _RESOLVED_OUTCOMES,
                "artifact": (
                    {
                        "type": str(artifact.get("artifact_type") or task.produces),
                        "facts": facts,
                        "source_tool_call_id": source_tool_call_id,
                        "source_tool_name": source_tool_name,
                    }
                    if artifact or source_tool_call_id
                    else None
                ),
                "response_contract": _response_contract(
                    semantic_outcome,
                    str(artifact.get("artifact_type") or task.produces),
                    facts,
                    source_tool_name,
                ),
                "explanation": _outcome_explanation(
                    semantic_outcome,
                    status,
                    ledger_entry,
                    state,
                ),
            }
        )

    resolved = [task for task in tasks if task["resolved"]]
    unresolved = [task for task in tasks if not task["resolved"]]
    completion: TurnCompletion
    if tasks and not unresolved:
        completion = "full"
    elif resolved:
        completion = "partial"
    else:
        completion = "none"

    answerable_source_ids = list(
        dict.fromkeys(
            str(task["artifact"]["source_tool_call_id"])
            for task in resolved
            if isinstance(task.get("artifact"), Mapping)
            and task["artifact"].get("source_tool_call_id")
        )
    )
    unavailable_parts = [str(task["question"]) for task in unresolved]
    return {
        "rewritten_query": rewritten_query,
        "aggregation_contract": {
            "coverage_target": rewritten_query,
            "required": [
                "用 rewritten_query 检查聚合后的回复是否覆盖用户这一轮的完整目标",
                "用各 Task 的 question、semantic_outcome 和 artifact.facts 决定具体回答内容",
            ],
            "forbidden": [
                "把 rewritten_query 当成业务事实来源",
                "用 rewritten_query 覆盖、补写或改变 Task 与 Tool Result",
            ],
        },
        "completion": completion,
        "tasks": tasks,
        "resolved_task_ids": [str(task["task_id"]) for task in resolved],
        "unresolved_task_ids": [str(task["task_id"]) for task in unresolved],
        "answerable_source_tool_call_ids": answerable_source_ids,
        "unavailable_parts": unavailable_parts,
        "outcome_counts": dict(Counter(str(task["semantic_outcome"]) for task in tasks)),
        "recommended_control_action": _recommended_control_action(
            completion,
            unresolved,
        ),
        "late_handoff_policy": (
            "Answer 阶段不得改变 boundary 或触发前端人工模式。只有当未完成 Task 看起来可能是在"
            "请求人工办理、但语义仍不明确时，才设置 offer_handoff_confirmation=true；response "
            "不得自行写确认问句或声称已经转接、提交、记录或办理，固定问句由 Runtime 渲染。"
        ),
    }


def answerable_source_tool_call_ids(state: Mapping[str, Any]) -> list[str]:
    return list(build_answer_context(state)["answerable_source_tool_call_ids"])


def resolved_answer_task_ids(state: Mapping[str, Any]) -> list[str]:
    return list(build_answer_context(state)["resolved_task_ids"])


def _semantic_outcome(
    status: Mapping[str, Any],
    artifact: Mapping[str, Any],
    ledger_entry: Mapping[str, Any],
    state: Mapping[str, Any],
) -> TaskSemanticOutcome:
    if artifact.get("usable"):
        return "answered_with_facts"

    task_status = str(status.get("status") or "")
    if task_status == "blocked" and status.get("user_can_supply") is True:
        return "needs_clarification"

    outcome = str(ledger_entry.get("outcome") or "")
    if not outcome:
        status_reason = str(status.get("reason") or "")
        if status_reason.startswith("tool_outcome:"):
            outcome = status_reason.partition(":")[2]
    if outcome in {"empty", "not_found"}:
        return "answered_no_match"
    if outcome == "unsupported":
        return "unsupported_capability"
    if outcome == "insufficient":
        return "insufficient_evidence"
    if outcome == "error":
        code = _tool_error_code(state, str(ledger_entry.get("tool_call_id") or ""))
        if code in {"timeout", "dependency_unavailable", "unauthorized"}:
            return "temporarily_unavailable"
        return "insufficient_evidence"

    if task_status == "blocked":
        return "blocked_dependency"
    if task_status == "failed":
        return "insufficient_evidence"
    if task_status == "unavailable":
        artifact_reason = str(artifact.get("reason") or "")
        if any(marker in artifact_reason for marker in ("no_matching", "not_found")):
            return "answered_no_match"
        return "insufficient_evidence"
    return "incomplete"


def _response_contract(
    outcome: TaskSemanticOutcome,
    artifact_type: str,
    facts: Any,
    source_tool_name: str,
) -> dict[str, Any]:
    if outcome == "answered_no_match":
        return {
            "required": ["明确说明当前查询没有找到匹配结果"],
            "forbidden": ["把正常空结果描述成系统故障", "编造替代结果"],
        }
    if outcome == "unsupported_capability":
        return {
            "required": ["说明当前工具或数据能力不支持该问题"],
            "forbidden": ["用现有字段推断不受支持的结论"],
        }
    if outcome == "needs_clarification":
        return {
            "required": ["只提出一个能够补齐必要信息的具体问题"],
            "forbidden": ["假设用户未提供的信息"],
        }
    if outcome == "temporarily_unavailable":
        return {
            "required": ["说明对应信息暂时无法查询"],
            "forbidden": ["描述为查无结果或能力不支持"],
        }
    if outcome in {"insufficient_evidence", "blocked_dependency", "incomplete"}:
        return {
            "required": ["说明现有结果不足以支持该问题的结论"],
            "forbidden": ["把无关或不完整结果当成答案"],
        }

    fact_mapping = _mapping(facts)
    if artifact_type == "facets":
        items = [
            item for item in fact_mapping.get("items", []) if isinstance(item, Mapping)
        ]
        values = [str(item.get("value")) for item in items if item.get("value")]
        count_scopes = {
            str(item.get("count_scope") or "sku")
            for item in items
            if item.get("count") is not None
        }
        return {
            "required": [
                "直接回答用户询问的目录选项",
                "列出 items 中的每个 value",
            ],
            "must_include_values": values,
            "count_semantics": (
                "每个 count 按该 item 的 count_scope 统计；spu 表示商品系列数，"
                "sku 表示具体版本数。sku_count 和 spu_count 是两个可核对的明确口径。"
            ),
            "count_scopes": sorted(count_scopes),
            "forbidden": [
                "只汇总 count 而省略 value",
                "把 count 总和称为商品系列数或品牌数",
            ],
        }
    if artifact_type in {"products", "ranked_product"}:
        contract = {
            "required": [
                "用返回商品的真实字段回答筛选、推荐或排名问题",
                "推荐理由必须逐项能在商品事实中找到依据",
            ],
            "forbidden": ["补写未返回的规格、用途认证或适配保证"],
        }
        products = [
            item
            for item in fact_mapping.get("products", [])
            if isinstance(item, Mapping)
        ]
        result_purpose = str(fact_mapping.get("result_purpose") or "search")
        selection_scope = str(fact_mapping.get("selection_scope") or "spu")
        total_match_count = _non_negative_int(fact_mapping.get("total_match_count"))
        returned_count = _non_negative_int(fact_mapping.get("returned_count"))
        is_exhaustive = bool(fact_mapping.get("is_exhaustive", True))
        contract["result_window"] = {
            "result_purpose": result_purpose,
            "selection_scope": selection_scope,
            "total_match_count": total_match_count,
            "returned_count": returned_count,
            "is_exhaustive": is_exhaustive,
        }
        if not is_exhaustive:
            scope_label = "具体版本/SKU" if selection_scope == "sku" else "商品系列/SPU"
            if result_purpose != "ranking":
                contract["required"].append(
                    f"明确共匹配 {total_match_count} 个{scope_label}，本次返回 "
                    f"{returned_count} 个候选，不得把候选数写成全部数量"
                )
            contract["forbidden"].append(
                f"声称商城只有本次返回的 {returned_count} 个结果"
            )
        if result_purpose == "recommendation":
            contract["required"].append(
                "products 已按推荐顺序排列；直接把第一项作为首选，其余项只作为备选"
            )
        elif result_purpose == "search":
            contract["required"].append(
                "把 products 表达为搜索结果，不得把第一项自动称为最推荐"
            )
        elif result_purpose == "lookup":
            contract["required"].append(
                "围绕已识别商品回答事实或版本信息，不得把返回窗口误称为完整商品集合"
            )
        elif result_purpose == "ranking":
            contract["required"].append(
                "按 query_plan.ranking 与商品 ranking_value 回答确定性名次"
            )
        if any(
            item.get("entity_scope") == "spu" or item.get("ranking_scope") == "spu"
            for item in products
        ):
            contract["required"].extend(
                [
                    "把返回对象作为商品系列/SPU，使用 spu_title 作为系列名称",
                    "价格使用 series_min_price/series_max_price，库存使用 series_total_stock",
                    "规格只使用 series_common_specs、series_option_specs 和 series_variants",
                ]
            )
            if any(item.get("ranking_scope") == "spu" for item in products):
                contract["required"].append(
                    "明确这是商品系列/SPU 排名，并使用 ranking_value 说明排名依据"
                )
            contract["forbidden"].extend(
                [
                    "把辅助 SKU 的 stock 当成系列总库存",
                    "把辅助 SKU 的 specs 当成全系列共同规格",
                    "把辅助 SKU 的 title 当成系列名称（有 spu_title 时使用 spu_title）",
                ]
            )
        return contract
    if artifact_type == "comparison":
        if fact_mapping.get("comparison_level") == "spu":
            return {
                "required": [
                    "按系列逐项比较至少两个 SPU 的价格区间、库存覆盖和规格选项",
                    "区分全系列共同规格与仅部分 SKU 提供的可选规格",
                    "只把 variants 中真实存在的 SKU 规格组合描述为可购买版本",
                ],
                "forbidden": [
                    "用单个代表 SKU 的规格概括整个系列",
                    "把不同可选规格自由组合成不存在的 SKU",
                    "把缺失字段补写成确定事实",
                ],
            }
        return {
            "required": ["围绕用户要求的比较维度逐项比较至少两款商品"],
            "forbidden": ["把缺失字段补写成确定事实"],
        }
    if artifact_type == "order":
        facts = fact_mapping
        query_mode = str(facts.get("query_mode") or "")
        total = facts.get("total_match_count")
        returned = facts.get("returned_count")
        if query_mode == "count":
            return {
                "required": [f"明确回答用户共有 {total or 0} 个订单"],
                "forbidden": [
                    "把订单候选列表数量当成总订单数",
                    "声称修改、取消、催促或办理了订单操作",
                ],
            }
        if query_mode == "analysis":
            exhaustive = bool(facts.get("is_exhaustive", False))
            required = [
                "把 analysis_orders 仅作为分析当前问题的证据，只回答用户实际询问的结论",
                "只引用支撑结论所必需的订单、商品、状态、时间或物流字段",
                "根据订单商品名称及规格识别用户提到的相关商品，不要求逐字完全相同",
            ]
            if not exhaustive:
                required.append("明确说明当前订单事实不完整，不能给出覆盖全部订单的否定结论")
            return {
                "required": required,
                "forbidden": [
                    "把 analysis_orders 中的全部订单逐项复述给用户",
                    "展示与用户问题无关的订单号、商品、金额、状态或物流",
                    "在 is_exhaustive=false 时声称用户从未买过某商品或不存在某类订单",
                    "声称修改、取消、催促或办理了订单操作",
                ],
            }
        if query_mode == "page" and returned == 0 and total:
            return {
                "required": [f"明确说明已经列完全部 {total} 个订单，没有更多下一页"],
                "forbidden": [
                    "声称用户没有任何订单",
                    "声称修改、取消、催促或办理了订单操作",
                ],
            }
        if facts.get("candidates"):
            required = [
                f"明确说明用户共有 {total or 0} 个订单",
                f"列出本次返回的 {returned or 0} 个订单摘要，不自动选择第一单",
            ]
            if not facts.get("is_exhaustive", True):
                required.append("明确说明这只是部分结果，并提示用户可以继续查看")
            return {
                "required": required,
                "forbidden": [
                    "把本次返回数量说成总订单数",
                    "声称修改、取消、催促或办理了订单操作",
                ],
            }
        return {
            "required": ["明确回答订单、状态、明细或物流问题"],
            "forbidden": ["声称修改、取消、催促或办理了订单操作"],
        }
    if artifact_type == "documents":
        return {
            "required": ["只根据返回文档内容归纳答案"],
            "forbidden": ["用模型常识补写政策、时限或承诺"],
        }
    return {
        "required": ["直接回答当前 Task 的 question"],
        "forbidden": [f"把 {source_tool_name or 'Tool'} 的无关字段当成核心答案"],
    }


def _outcome_explanation(
    outcome: TaskSemanticOutcome,
    status: Mapping[str, Any],
    ledger_entry: Mapping[str, Any],
    state: Mapping[str, Any],
) -> str:
    explanations = {
        "answered_with_facts": "Tool 返回了可直接支持当前 Task 的结构化事实。",
        "answered_no_match": "Tool 正常完成查询，但没有找到匹配结果；这是可靠的否定结论。",
        "needs_clarification": "缺少用户能够补充的必要信息。",
        "unsupported_capability": "Tool 已确认当前数据或查询能力不能支持该问题。",
        "temporarily_unavailable": "Tool 或其依赖暂时不可用。",
        "insufficient_evidence": "当前 Observation 不足以支持可靠结论。",
        "blocked_dependency": "前置 Task 没有产生可用 Artifact，后续 Task 无法执行。",
        "incomplete": "Task 尚未形成可用于回答的终态。",
    }
    base = explanations[outcome]
    missing = status.get("missing_information")
    if outcome == "needs_clarification" and isinstance(missing, list) and missing:
        return f"{base} 需要补充：{'、'.join(str(item) for item in missing)}。"
    if outcome == "temporarily_unavailable":
        code = _tool_error_code(state, str(ledger_entry.get("tool_call_id") or ""))
        return f"{base} 错误类型：{code or 'execution_error'}。"
    return base


def _recommended_control_action(
    completion: TurnCompletion,
    unresolved: list[dict[str, Any]],
) -> str:
    if completion == "full":
        return "finish_answer"
    if completion == "partial":
        return "finish_partial"
    if unresolved and all(
        task["semantic_outcome"] == "needs_clarification" for task in unresolved
    ):
        return "ask_clarification"
    return "finish_unavailable"


def _latest_active_ledger_by_task(
    state: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for entry in state.get("subquery_ledger", []):
        if not isinstance(entry, Mapping) or not is_active_ledger_entry(entry):
            continue
        task_id = str(entry.get("subquery") or "").strip()
        if task_id:
            result[task_id] = entry
    return result


def _tool_error_code(state: Mapping[str, Any], call_id: str) -> str:
    for result in reversed(state.get("tool_results", [])):
        if not isinstance(result, Mapping):
            continue
        if str(result.get("tool_call_id") or "") != call_id:
            continue
        execution = _mapping(result.get("execution"))
        error = _mapping(execution.get("error"))
        return str(error.get("code") or "")
    return ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _route_rewritten_query(state: Mapping[str, Any]) -> str:
    route_plan = state.get("route_plan")
    if isinstance(route_plan, Mapping):
        return str(route_plan.get("rewritten_query") or "").strip()
    return str(getattr(route_plan, "rewritten_query", "") or "").strip()


__all__ = [
    "answerable_source_tool_call_ids",
    "build_answer_context",
    "resolved_answer_task_ids",
]
