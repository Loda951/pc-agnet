"""Business Tool wave execution and persistence."""

from typing import Any

from langgraph.config import get_stream_writer
from pydantic import ValidationError

from app.agent.artifacts import bound_task_catalog_targets
from app.agent.decisions import OrchestratorDecision
from app.agent.outcomes import tool_call_fingerprint
from app.agent.projections import apply_tool_output
from app.agent.route_runtime import _resolve_compare_sku_ids
from app.agent.routing import tool_planning_subqueries
from app.agent.state import AgentState
from app.agent.tool_loop import _find_reusable_tool_result
from app.repositories.conversations import ConversationRepository
from app.tools.schemas import ToolError, ToolExecutionResult


async def execute_tool_wave(
    runtime: Any,
    state: AgentState,
    *,
    repository_factory: Any = ConversationRepository,
    stream_writer_factory: Any = get_stream_writer,
) -> AgentState:
    decision = OrchestratorDecision.model_validate(state["decision"])
    repo = repository_factory(runtime.session)
    writer = stream_writer_factory()
    calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    next_wave = state.get("tool_wave_count", 0) + 1

    for planned_call in decision.tool_calls:
        task_state = state.setdefault("task_status", {}).get(planned_call.subquery)
        if isinstance(task_state, dict):
            task_state.update(
                status="running",
                reason="tool_call_dispatched",
                wave=next_wave,
            )

    for planned_call in decision.tool_calls:
        call = planned_call
        execution: ToolExecutionResult | None = None
        reused_from_tool_call_id: str | None = None
        try:
            call, applied_memory_ids = runtime._prepare_tool_call(state, planned_call)
        except ValidationError as exc:
            execution = ToolExecutionResult(
                tool_name=call.name,
                ok=False,
                error=ToolError(
                    code="invalid_input",
                    message=str(exc),
                    retryable=True,
                    recommended_action="replan_arguments",
                ),
            )
        else:
            if applied_memory_ids:
                state["applied_memory_ids"] = list(
                    dict.fromkeys(
                        [*state.get("applied_memory_ids", []), *applied_memory_ids]
                    )
                )
            reusable = _find_reusable_tool_result(state, call)
            if reusable is not None:
                reused_from_tool_call_id = str(reusable["tool_call_id"])
                execution = ToolExecutionResult.model_validate(reusable["execution"])
        writer(
            {
                "kind": "tool_call",
                "tool_name": call.name,
                "status": "started",
                "input": call.arguments,
            }
        )
        if execution is None:
            contract = runtime.contract_provider.get_contract(call.name)
            if contract is None:
                execution = ToolExecutionResult(
                    tool_name=call.name,
                    ok=False,
                    error=ToolError(
                        code="unknown_tool", message=f"unknown tool: {call.name}"
                    ),
                )
            else:
                try:
                    execution = await runtime.tool_executor.execute(
                        contract,
                        call.arguments,
                        _runtime_context_for_call(state, call),
                    )
                except ValidationError as exc:
                    execution = ToolExecutionResult(
                        tool_name=call.name,
                        ok=False,
                        error=ToolError(
                            code="invalid_input",
                            message=str(exc),
                            retryable=True,
                            recommended_action="replan_arguments",
                        ),
                    )
                except Exception as exc:  # defensive orchestration boundary
                    execution = ToolExecutionResult(
                        tool_name=call.name,
                        ok=False,
                        error=ToolError(code=type(exc).__name__, message=str(exc)),
                    )

        call_json = call.model_dump(mode="json")
        call_json["fingerprint"] = tool_call_fingerprint(call.name, call.arguments)
        execution_json = execution.model_dump(mode="json")
        calls.append(call_json)
        result_json = {
            "tool_call_id": call.id,
            "name": call.name,
            "execution": execution_json,
        }
        if reused_from_tool_call_id is not None:
            result_json["reused_from_tool_call_id"] = reused_from_tool_call_id
        results.append(result_json)
        writer(
            {
                "kind": "tool_call",
                "tool_name": call.name,
                "status": "completed" if execution.ok else "error",
                "input": call.arguments,
                "output": execution_json,
            }
        )
        if reused_from_tool_call_id is None:
            await repo.add_tool_call(
                state["run_id"],
                call.name,
                call.arguments,
                execution_json,
            )
        apply_tool_output(state, call, execution)

    wave = {
        "wave": next_wave,
        "calls": calls,
        "results": results,
    }
    state.setdefault("tool_waves", []).append(wave)
    state.setdefault("tool_results", []).extend(results)
    state["tool_wave_count"] = wave["wave"]
    return state


def _runtime_context_for_call(
    state: AgentState,
    call: Any,
) -> dict[str, Any]:
    context: dict[str, Any] = {"user_id": state["user_id"]}
    if call.name not in {"catalog_search", "catalog_compare"}:
        return context
    context["targets"] = []
    task = next(
        (
            item
            for item in tool_planning_subqueries(state.get("route_plan"))
            if item.id == str(call.subquery).strip()
        ),
        None,
    )
    if task is not None:
        context["targets"] = bound_task_catalog_targets(state, task)
        if not context["targets"] and not task.input_requirements:
            context["targets"] = _fallback_runtime_catalog_targets(state, call)
        return context
    context["targets"] = _fallback_runtime_catalog_targets(state, call)
    return context


def _fallback_runtime_catalog_targets(
    state: AgentState,
    call: Any,
) -> list[dict[str, Any]]:
    """Ground legacy/fallback routed calls without exposing identities as Tool arguments."""
    snapshot = state.get("working_memory_snapshot") or state.get("working_memory") or {}
    catalog = snapshot.get("catalog") if isinstance(snapshot, dict) else None
    if not isinstance(catalog, dict):
        return []

    if call.name == "catalog_compare":
        sku_ids = _resolve_compare_sku_ids(state["message"], snapshot)
        candidate_skus = catalog.get("candidate_sku_ids")
        candidate_spus = catalog.get("candidate_spu_ids")
        spu_by_sku = (
            {
                sku_id: candidate_spus[index]
                for index, sku_id in enumerate(candidate_skus)
                if isinstance(sku_id, int)
                and isinstance(candidate_spus, list)
                and index < len(candidate_spus)
                and isinstance(candidate_spus[index], int)
            }
            if isinstance(candidate_skus, list)
            else {}
        )
        return [
            {
                "sku_id": sku_id,
                **(
                    {"spu_id": spu_by_sku[sku_id]}
                    if sku_id in spu_by_sku
                    else {}
                ),
                "source": "comparison_context",
            }
            for sku_id in sku_ids
        ]

    query = str(call.arguments.get("query") or "")
    if not any(marker in query for marker in ("这个", "这款", "该商品", "该款")):
        return []
    sku_id = catalog.get("referenced_sku_id")
    spu_id = catalog.get("referenced_spu_id")
    candidate_skus = catalog.get("candidate_sku_ids")
    candidate_spus = catalog.get("candidate_spu_ids")
    if not isinstance(sku_id, int) and isinstance(candidate_skus, list):
        valid_skus = [item for item in candidate_skus if isinstance(item, int)]
        sku_id = valid_skus[0] if len(valid_skus) == 1 else None
    if not isinstance(spu_id, int) and isinstance(candidate_spus, list):
        valid_spus = [item for item in candidate_spus if isinstance(item, int)]
        spu_id = valid_spus[0] if len(valid_spus) == 1 else None
    if not isinstance(sku_id, int) and not isinstance(spu_id, int):
        return []
    return [
        {
            **({"sku_id": sku_id} if isinstance(sku_id, int) else {}),
            **({"spu_id": spu_id} if isinstance(spu_id, int) else {}),
            "source": "working_memory_reference",
        }
    ]
