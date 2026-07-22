"""Tool planning, wave validation, and terminal decision helpers."""

import copy
import json
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.decisions import OrchestratorDecision, PlannedToolCall, infer_tool_subquery
from app.agent.limits import MAX_TOOL_WAVES
from app.agent.outcomes import (
    build_subquery_ledger,
    is_active_ledger_entry,
    normalize_tool_result,
    query_fingerprint,
    tool_call_fingerprint,
)
from app.agent.prompts import (
    build_orchestrator_system_prompt,
    build_orchestrator_user_prompt,
)
from app.agent.responses import (
    _active_tool_results,
    _fallback_answer,
    _fallback_unavailable_answer,
    _latest_successful_tool_output,
    _usable_tool_call_ids,
)
from app.agent.routing import tool_planning_subqueries
from app.agent.state import AgentState
from app.tools.contracts import LLM_SAFE_TOOL_NAMES, ToolContract


def _orchestrator_messages(
    state: AgentState,
    call_count: int,
) -> list[SystemMessage | HumanMessage | AIMessage | ToolMessage]:
    messages: list[SystemMessage | HumanMessage | AIMessage | ToolMessage] = [
        SystemMessage(
            content=build_orchestrator_system_prompt(
                tool_waves=state.get("tool_waves", []),
                tool_results=state.get("tool_results", []),
            )
        )
    ]
    route_plan = state["route_plan"]
    routed_subqueries = [
        {"id": item.id, "canonical_query": item.query}
        for item in tool_planning_subqueries(route_plan)
    ]
    messages.append(
        HumanMessage(
            content=build_orchestrator_user_prompt(
                message=None,
                tool_wave_count=state.get("tool_wave_count", 0),
                orchestrator_call_count=call_count,
                memory_context=None,
                routed_subqueries=routed_subqueries,
                subquery_ledger=state.get("subquery_ledger", []),
                terminal_guard_feedback=state.get("terminal_guard_feedback"),
            )
        )
    )
    for wave in state.get("tool_waves", []):
        messages.append(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": call["id"],
                        "name": call["name"],
                        "args": {
                            **call["arguments"],
                            "subquery": (
                                str(call.get("subquery") or "").strip()
                                or infer_tool_subquery(call["name"], call["arguments"])
                            ),
                        },
                        "type": "tool_call",
                    }
                    for call in wave.get("calls", [])
                ],
            )
        )
        for result in wave.get("results", []):
            messages.append(
                ToolMessage(
                    content=json.dumps(result["execution"], ensure_ascii=False),
                    tool_call_id=result["tool_call_id"],
                    name=result["name"],
                )
            )
    return messages


def _tool_decision(name: str, arguments: dict[str, Any]) -> OrchestratorDecision:
    return OrchestratorDecision(
        type="tool_calls",
        tool_calls=[
            PlannedToolCall(
                id=f"fallback_{name}",
                name=name,
                arguments=arguments,
                subquery=infer_tool_subquery(name, arguments),
            )
        ],
    )


def _orchestrator_business_tool_definition(contract: ToolContract) -> dict[str, Any]:
    definition = copy.deepcopy(contract.as_llm_tool())
    function = definition["function"]
    function["description"] = (
        f"{function['description']} The runtime injects the frozen routed canonical query; "
        "do not provide or rewrite a query field."
    )
    parameters = function["parameters"]
    properties = parameters.setdefault("properties", {})
    properties.pop("query", None)
    properties["subquery"] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 300,
        "description": (
            "Copy the sq_n ID of exactly one routed subquery. Do not invent, rewrite, merge, "
            "or split routed subqueries. Keep the same ID across dependent or recovery waves."
        ),
    }
    required = parameters.setdefault("required", [])
    if "query" in required:
        required.remove("query")
    if "subquery" not in required:
        required.append("subquery")
    return definition


def _constrain_calls_to_route_plan(
    state: AgentState,
    calls: list[PlannedToolCall],
) -> list[PlannedToolCall]:
    routed = {
        item.id: item for item in tool_planning_subqueries(state.get("route_plan"))
    }
    only_routed = next(iter(routed.values())) if len(routed) == 1 else None
    constrained: list[PlannedToolCall] = []
    for call in calls:
        raw_subquery = call.subquery.strip()
        subquery = routed.get(raw_subquery)
        if (
            subquery is None
            and only_routed is not None
            and not re.fullmatch(r"sq_\d+", raw_subquery, flags=re.IGNORECASE)
        ):
            # When only one admitted task exists, a missing subquery ID is unambiguous. Older
            # model responses may contain an inferred natural-language label here instead of
            # the sq_n metadata. Explicit unknown sq_n IDs remain rejected.
            subquery = only_routed
        if subquery is None:
            continue
        arguments = dict(call.arguments)
        # The Router owns query rewrite. Never ask the model to reproduce trusted canonical
        # text byte-for-byte; overwrite any model-provided query before public input validation.
        arguments["query"] = subquery.query
        constrained.append(
            call.model_copy(
                update={
                    "arguments": arguments,
                    "subquery": subquery.id,
                }
            )
        )
    return constrained


def _unique_tool_call_ids(
    calls: list[PlannedToolCall],
    previous_waves: list[dict[str, Any]],
    call_count: int,
) -> list[PlannedToolCall]:
    used_ids = {
        str(call.get("id"))
        for wave in previous_waves
        for call in wave.get("calls", [])
        if isinstance(call, dict) and call.get("id")
    }
    normalized: list[PlannedToolCall] = []
    for index, call in enumerate(calls, start=1):
        call_id = call.id
        if not call_id or call_id in used_ids:
            base_id = f"call_{call_count}_{index}"
            call_id = base_id
            suffix = 1
            while call_id in used_ids:
                suffix += 1
                call_id = f"{base_id}_{suffix}"
        used_ids.add(call_id)
        normalized.append(call.model_copy(update={"id": call_id}))
    return normalized


def _find_reusable_tool_result(
    state: AgentState,
    call: PlannedToolCall,
) -> dict[str, Any] | None:
    fingerprint = tool_call_fingerprint(call.name, call.arguments)
    matches: list[dict[str, Any]] = []
    for wave in state.get("tool_waves", []):
        calls = {
            str(previous.get("id") or ""): previous
            for previous in wave.get("calls", [])
            if isinstance(previous, dict)
        }
        for result in wave.get("results", []):
            if not isinstance(result, dict) or result.get("name") != call.name:
                continue
            previous_call = calls.get(str(result.get("tool_call_id") or ""), {})
            previous_arguments = previous_call.get("arguments", {})
            previous_fingerprint = str(previous_call.get("fingerprint") or "")
            if not previous_fingerprint and isinstance(previous_arguments, dict):
                previous_fingerprint = tool_call_fingerprint(call.name, previous_arguments)
            if previous_fingerprint == fingerprint:
                matches.append(result)

    if not matches:
        return None

    outcomes = [normalize_tool_result(result) for result in matches]
    if outcomes[-1].outcome != "error":
        return matches[-1]
    # One retry is allowed after the first execution error; later identical calls reuse it.
    if sum(outcome.outcome == "error" for outcome in outcomes) >= 2:
        return matches[-1]
    return None


def _planner_requires_business_tools(state: AgentState) -> bool:
    """Return whether an observation turn can still make an admissible business call."""
    if state.get("tool_wave_count", 0) >= MAX_TOOL_WAVES:
        return False

    routed_ids = {
        item.id for item in tool_planning_subqueries(state.get("route_plan"))
    }
    called_ids = {
        str(call.get("subquery") or "").strip()
        for wave in state.get("tool_waves", [])
        for call in wave.get("calls", [])
        if isinstance(call, dict)
    }
    called_ids.update(
        str(entry.get("subquery") or "").strip()
        for entry in state.get("subquery_ledger", [])
        if isinstance(entry, dict)
    )
    if routed_ids - called_ids:
        return True

    active_ledger = [
        entry
        for entry in state.get("subquery_ledger", [])
        if is_active_ledger_entry(entry)
    ]
    if any(entry.get("status") == "failed" for entry in active_ledger):
        return True

    tool_names = {str(entry.get("tool_name") or "") for entry in active_ledger}
    if (
        "catalog_search" in tool_names
        and "catalog_compare" not in tool_names
        and _request_explicitly_requires_comparison(state.get("message", ""))
    ):
        return True
    return any(
        entry.get("tool_name") == "order_lookup"
        and entry.get("result_type") == "order_candidates"
        for entry in active_ledger
    )


def _all_planned_subqueries_usable(state: AgentState) -> bool:
    """Return whether every routed Tool task has active usable evidence."""
    routed_ids = {
        item.id for item in tool_planning_subqueries(state.get("route_plan"))
    }
    if not routed_ids:
        return False
    usable_ids = {
        str(entry.get("subquery") or "").strip()
        for entry in state.get("subquery_ledger", [])
        if is_active_ledger_entry(entry)
        and entry.get("outcome") == "usable"
        and entry.get("has_usable_information")
    }
    return routed_ids <= usable_ids


def _followup_tool_call_allowed(state: AgentState, call: PlannedToolCall) -> bool:
    """Admit only same-tool recovery or an original-request dependent call."""
    ledger = state.get("subquery_ledger", [])
    if not ledger and state.get("tool_waves"):
        ledger = [
            entry.model_dump(mode="json")
            for entry in build_subquery_ledger(state.get("tool_waves", []))
        ]

    subquery = (call.subquery or infer_tool_subquery(call.name, call.arguments)).strip()
    key = subquery.casefold()
    matching = [
        entry
        for entry in ledger
        if is_active_ledger_entry(entry)
        and str(entry.get("subquery") or "").strip().casefold() == key
    ]
    if not matching:
        return _is_supported_dependent_call(state, call)

    latest = matching[-1]
    if query_fingerprint(call.arguments.get("query")) != str(
        latest.get("query_fingerprint") or query_fingerprint(
            latest.get("canonical_query")
            or latest.get("arguments", {}).get("query")
        )
    ):
        return False

    outcome = str(latest.get("outcome") or "")
    if (
        outcome == "usable"
        and call.name != str(latest.get("tool_name") or "")
        and _is_supported_dependent_call(state, call)
    ):
        # A usable discovery result may be the prerequisite for a different Tool in the same
        # routed task, for example catalog_search -> catalog_compare. Do not mark the whole
        # subquery complete before admitting that explicitly requested dependent step.
        return True
    if outcome in {"usable", "empty", "not_found", "unsupported", "insufficient"}:
        return False

    previous_fingerprint = str(latest.get("fingerprint") or "")
    next_fingerprint = tool_call_fingerprint(call.name, call.arguments)
    if outcome != "error":
        return False

    result = _tool_result_by_call_id(state, str(latest.get("tool_call_id") or ""))
    error = result.get("execution", {}).get("error", {}) if result else {}
    if not isinstance(error, dict):
        return False
    code = str(error.get("code") or "execution_error")
    action = str(error.get("recommended_action") or "")
    if not action:
        action = {
            "invalid_input": "replan_arguments",
            "timeout": "retry_once",
        }.get(code, "stop")

    if action == "retry_once":
        same_failures = sum(
            entry.get("outcome") == "error"
            and str(entry.get("fingerprint") or "") == previous_fingerprint
            for entry in ledger
        )
        return same_failures < 2 and next_fingerprint == previous_fingerprint
    if action == "replan_arguments":
        if call.name != str(latest.get("tool_name") or ""):
            return False
        attempts = sum(
            entry.get("outcome") == "error"
            and str(entry.get("subquery") or "").strip().casefold() == key
            for entry in ledger
        )
        return attempts < 2 and next_fingerprint != previous_fingerprint
    return False


def _is_supported_dependent_call(state: AgentState, call: PlannedToolCall) -> bool:
    """Allow only bounded continuations that the original request already requires."""
    active = [
        entry
        for entry in state.get("subquery_ledger", [])
        if is_active_ledger_entry(entry) and entry.get("has_usable_information")
    ]
    if call.name == "catalog_compare":
        if not any(entry.get("tool_name") == "catalog_search" for entry in active):
            return False
        if not _request_explicitly_requires_comparison(state.get("message", "")):
            return False
        requested_sku_ids = call.arguments.get("sku_ids")
        if not isinstance(requested_sku_ids, list):
            return False
        normalized_sku_ids = {
            value
            for value in requested_sku_ids
            if isinstance(value, int) and not isinstance(value, bool)
        }
        if len(normalized_sku_ids) < 2:
            return False
        return normalized_sku_ids <= _active_catalog_search_sku_ids(state)
    if call.name == "order_lookup":
        output = _latest_successful_tool_output(state, "order_lookup")
        if not output or output.get("result_type") != "order_candidates":
            return False
        order_id = call.arguments.get("order_id")
        if not isinstance(order_id, int) or isinstance(order_id, bool):
            return False
        candidates = output.get("candidates")
        if not isinstance(candidates, list):
            return False
        candidate_ids = {
            item.get("id")
            for item in candidates
            if isinstance(item, dict)
            and isinstance(item.get("id"), int)
            and not isinstance(item.get("id"), bool)
        }
        return order_id in candidate_ids
    return False


def _request_explicitly_requires_comparison(message: str) -> bool:
    compact = re.sub(r"\s+", "", message.casefold())
    return any(
        marker in compact
        for marker in (
            "对比",
            "比较",
            "区别",
            "差异",
            "差别",
            "哪个好",
            "哪款更",
            "哪个更",
            "选哪个",
            "怎么选",
            "compare",
            "comparison",
            "difference",
            "whichisbetter",
        )
    )


def _active_catalog_search_sku_ids(state: AgentState) -> set[int]:
    sku_ids: set[int] = set()
    for result in _active_tool_results(state):
        if result.get("name") != "catalog_search":
            continue
        execution = result.get("execution")
        if not isinstance(execution, dict) or not execution.get("ok"):
            continue
        output = execution.get("output")
        products = output.get("products") if isinstance(output, dict) else None
        if not isinstance(products, list):
            continue
        for product in products:
            sku_id = product.get("sku_id") if isinstance(product, dict) else None
            if isinstance(sku_id, int) and not isinstance(sku_id, bool):
                sku_ids.add(sku_id)
    return sku_ids


def _tool_result_by_call_id(state: AgentState, call_id: str) -> dict[str, Any] | None:
    for result in reversed(state.get("tool_results", [])):
        if str(result.get("tool_call_id") or "") == call_id:
            return result
    return None


def _unresolved_initial_subqueries(state: AgentState) -> list[str]:
    ledger = state.get("subquery_ledger", [])
    initial = {
        item.id.casefold(): item.query
        for item in tool_planning_subqueries(state.get("route_plan"))
    } or {
        str(entry.get("subquery") or "").strip().casefold(): str(
            entry.get("subquery") or ""
        ).strip()
        for entry in ledger
        if entry.get("wave") == 1 and str(entry.get("subquery") or "").strip()
    }
    resolved = {
        str(entry.get("subquery") or "").strip().casefold()
        for entry in ledger
        if is_active_ledger_entry(entry)
        and entry.get("has_usable_information")
        and str(entry.get("subquery") or "").strip()
    }
    return [label for key, label in initial.items() if key not in resolved]


def _clarification_decision(response: str, reason: str) -> OrchestratorDecision:
    return OrchestratorDecision(
        type="clarification",
        response=response,
        reason=reason,
        control_action="ask_clarification",
    )


def _plain_text_observation_decision(
    state: AgentState,
    message: AIMessage,
) -> OrchestratorDecision | None:
    """Recover a customer answer when an observation model omits the control Tool Call."""
    if message.tool_calls or not state.get("tool_results"):
        return None
    response = _ai_message_text(message).strip()
    usable_ids = _usable_tool_call_ids(state)
    if not response or not usable_ids:
        return None

    unresolved = _unresolved_initial_subqueries(state)
    if unresolved:
        return OrchestratorDecision(
            type="partial_response",
            response=response,
            reason="plain_text_observation_recovery",
            control_action="finish_partial",
            used_tool_call_ids=usable_ids,
            unavailable_parts=unresolved,
        )
    return OrchestratorDecision(
        type="grounded_response",
        response=response,
        reason="plain_text_observation_recovery",
        control_action="finish_answer",
        used_tool_call_ids=usable_ids,
    )


def _ai_message_text(message: AIMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def _state_terminal_decision(state: AgentState, reason: str) -> OrchestratorDecision:
    """Stop the loop without discarding observations already useful to the user."""
    usable_ids = _usable_tool_call_ids(state)
    unresolved = _unresolved_initial_subqueries(state)
    if usable_ids:
        response = _fallback_answer(state)
        if unresolved:
            response = (
                f"{response}\n\n"
                f"暂时未能完成：{'、'.join(unresolved)}。你可以稍后重试或补充更具体的信息。"
            )
            return OrchestratorDecision(
                type="partial_response",
                response=response,
                reason=reason,
                control_action="finish_partial",
                used_tool_call_ids=usable_ids,
                unavailable_parts=unresolved,
            )
        return OrchestratorDecision(
            type="grounded_response",
            response=response,
            reason=reason,
            control_action="finish_answer",
            used_tool_call_ids=usable_ids,
        )
    if state.get("tool_results"):
        return OrchestratorDecision(
            type="unavailable_response",
            response=_fallback_unavailable_answer(state),
            reason=reason,
            control_action="finish_unavailable",
            unavailable_parts=_unresolved_initial_subqueries(state)
            or ["请求所需的业务信息"],
        )
    return _clarification_decision(
        "我还不能准确判断你的需求。请补充具体商品、订单或想咨询的问题。",
        reason,
    )


def _terminal_fallback_decision(
    state: AgentState,
    validation_reason: str,
) -> OrchestratorDecision:
    reason = f"terminal_guard_fallback:{validation_reason}"
    return _state_terminal_decision(state, reason)


def _tag_from_decision(
    decision: OrchestratorDecision,
    current_tag: str | None,
) -> str:
    if decision.type == "clarification":
        return current_tag or "clarification"

    tool_names = list(dict.fromkeys(call.name for call in decision.tool_calls))
    if tool_names:
        previous_tool_names = (
            current_tag.split(" + ")
            if current_tag and all(
                self_name in LLM_SAFE_TOOL_NAMES for self_name in current_tag.split(" + ")
            )
            else []
        )
        return " + ".join(dict.fromkeys([*previous_tool_names, *tool_names]))
    return current_tag or "general"
