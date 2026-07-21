import asyncio
import copy
import json
import re
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.decisions import (
    OrchestratorDecision,
    PlannedToolCall,
    control_tool_definitions,
    decision_from_ai_message,
    infer_tool_subquery,
)
from app.agent.intent import (
    boundary_for_classification,
    classify_boundary,
    classify_intent,
    extract_order_id,
    requires_security_refusal,
    requires_static_unsupported,
)
from app.agent.outcomes import (
    active_usable_tool_call_ids,
    build_subquery_ledger,
    is_active_ledger_entry,
    normalize_tool_result,
    query_fingerprint,
    tool_call_fingerprint,
    validate_terminal_decision,
)
from app.agent.prompts import (
    REQUEST_ROUTER_SYSTEM_PROMPT,
    build_orchestrator_system_prompt,
    build_orchestrator_user_prompt,
    build_request_router_user_prompt,
)
from app.agent.routing import (
    RequestRoutePlan,
    RoutedSubquery,
    blocked_subqueries,
    request_route_tool_definition,
    route_plan_from_ai_message,
    tool_planning_subqueries,
)
from app.agent.state import AgentState
from app.core.config import Settings
from app.core.llm import build_chat_model
from app.repositories.conversations import ConversationRepository
from app.schemas.catalog import ProductCard
from app.schemas.chat import (
    BoundaryClassification,
    ChatRequest,
    ChatResponse,
    EvidenceItem,
    SuggestedAction,
)
from app.schemas.order import OrderCard
from app.services.context import ConversationContextService, serialize_compact_audit
from app.services.memory import MemoryService
from app.tools.contracts import (
    LLM_SAFE_TOOL_NAMES,
    DefaultToolContractProvider,
    RegistryToolExecutor,
    ToolContract,
    ToolContractProvider,
    ToolExecutor,
)
from app.tools.schemas import (
    CatalogCompareInput,
    ToolError,
    ToolExecutionResult,
)

MAX_ORCHESTRATOR_CALLS = 3
MAX_TOOL_WAVES = 2
MAX_REQUEST_ROUTER_CALLS = 1

CUSTOMER_SPEC_LABELS = {
    "backlit": "背光",
    "channels": "声道",
    "color": "颜色",
    "connection_type": "连接方式",
    "enclosure_type": "耳罩类型",
    "field_of_view": "视野范围",
    "frame_rate": "帧率",
    "frequency_response": "频响范围",
    "hand_orientation": "持握方向",
    "max_dpi": "最高 DPI",
    "microphone": "麦克风",
    "panel_type": "面板类型",
    "power_w": "功率",
    "refresh_rate": "刷新率",
    "resolution": "分辨率",
    "response_time_ms": "响应时间",
    "size_inch": "尺寸",
    "style": "款式",
    "switches": "轴体",
    "tenkeyless": "键盘布局",
    "tracking_method": "传感方式",
    "type": "类型",
    "weight_g": "重量",
    "wireless": "无线连接",
}


class AgentRuntime:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        *,
        contract_provider: ToolContractProvider | None = None,
        tool_executor: ToolExecutor | None = None,
        chat_model: Any | None = None,
        router_model: Any | None = None,
        context_service: ConversationContextService | None = None,
        memory_service: MemoryService | None = None,
    ):
        self.session = session
        self.settings = settings
        self.contract_provider = contract_provider or DefaultToolContractProvider()
        self.tool_executor = tool_executor or RegistryToolExecutor(session, settings)
        self.context_service = context_service or ConversationContextService(session, settings)
        self.memory_service = memory_service or MemoryService()
        self.llm = chat_model if chat_model is not None else build_chat_model(settings)
        business_tools = [
            _orchestrator_business_tool_definition(contract)
            for contract in self.contract_provider.list_contracts()
        ]
        controls_by_name = {
            definition["function"]["name"]: definition
            for definition in control_tool_definitions()
        }
        observation_controls = [
            controls_by_name[name]
            for name in (
                "finish_answer",
                "finish_partial",
                "finish_unavailable",
                "ask_clarification",
            )
        ]
        router_base_model = router_model
        if router_base_model is None and chat_model is None:
            router_base_model = self.llm
        self.request_router = (
            router_base_model.bind_tools([request_route_tool_definition()])
            if router_base_model
            else None
        )
        self.orchestrator = (
            self.llm.bind_tools([*business_tools, *controls_by_name.values()])
            if self.llm
            else None
        )
        self.tool_planner = self.llm.bind_tools(business_tools) if self.llm else None
        self.answer_synthesizer = (
            self.llm.bind_tools(observation_controls) if self.llm else None
        )
        self.recovery_planner = (
            self.llm.bind_tools([*business_tools, *observation_controls])
            if self.llm
            else None
        )

    async def run(self, request: ChatRequest, user_id: int) -> ChatResponse:
        result: AgentState = await self._build_graph().ainvoke(
            self._initial_state(request, user_id)
        )
        return self._response_from_state(result)

    async def run_stream(
        self, request: ChatRequest, user_id: int
    ) -> AsyncIterator[dict[str, Any]]:
        state = self._initial_state(request, user_id)
        try:
            async for mode, update in self._build_graph().astream(
                state,
                stream_mode=["custom", "updates"],
            ):
                if mode == "custom":
                    event_kind = update.get("kind")
                    if event_kind == "tool_call":
                        yield _stream_event(
                            "tool_call",
                            state,
                            tool_name=update["tool_name"],
                            status=update["status"],
                            input=update.get("input"),
                            output=update.get("output"),
                        )
                    continue

                for node_name, node_state in update.items():
                    state.update(node_state)
                    if node_name == "load_context":
                        yield _stream_event("run_started", state)
                    elif node_name == "request_router":
                        yield _stream_event("boundary", state, boundary=state["boundary"])
                    elif node_name == "execute_tool_wave":
                        yield _context_event(state)
                    elif node_name in {
                        "finalize_response",
                        "render_handoff_template",
                        "render_out_of_scope_template",
                        "render_unsupported_template",
                        "render_security_template",
                        "render_clarification_template",
                        "render_direct_template",
                    }:
                        yield _stream_event("delta", state, delta=state["answer"])
                    elif node_name == "persist_turn":
                        yield _stream_event(
                            "done",
                            state,
                            response=self._response_from_state(state).model_dump(mode="json"),
                        )
        except asyncio.CancelledError:
            await self._mark_run_failed(state, "cancelled", "client disconnected")
            raise
        except Exception as exc:
            await self._mark_run_failed(state, type(exc).__name__, str(exc))
            yield _stream_event(
                "error",
                state,
                error_type=type(exc).__name__,
                message="AI 回复生成失败，请稍后重试。",
                retryable=True,
            )

    def _initial_state(self, request: ChatRequest, user_id: int) -> AgentState:
        return {
            "user_id": user_id,
            "conversation_id": request.conversation_id,
            "message": request.message,
            "request_router_call_count": 0,
            "orchestrator_call_count": 0,
            "tool_wave_count": 0,
            "tool_waves": [],
            "tool_results": [],
            "subquery_ledger": [],
            "terminal_guard_replan_count": 0,
            "parsed": {},
            "products": [],
            "evidence": [],
            "order": None,
        }

    def _build_graph(self):
        workflow = StateGraph(AgentState)
        workflow.add_node("load_context", self._load_context)
        workflow.add_node("request_router", self._request_route)
        workflow.add_node("orchestrate", self._orchestrate)
        workflow.add_node("execute_tool_wave", self._execute_tool_wave)
        workflow.add_node("normalize_tool_results", self._normalize_tool_results)
        workflow.add_node("update_subquery_ledger", self._update_subquery_ledger)
        workflow.add_node("terminal_guard", self._terminal_guard)
        workflow.add_node("finalize_response", self._finalize_response)
        workflow.add_node("render_handoff_template", self._render_handoff_template)
        workflow.add_node("render_out_of_scope_template", self._render_out_of_scope_template)
        workflow.add_node("render_unsupported_template", self._render_unsupported_template)
        workflow.add_node("render_security_template", self._render_security_template)
        workflow.add_node("render_clarification_template", self._render_clarification_template)
        workflow.add_node("render_direct_template", self._render_direct_template)
        workflow.add_node("persist_turn", self._persist_turn)

        workflow.set_entry_point("load_context")
        workflow.add_edge("load_context", "request_router")
        workflow.add_conditional_edges(
            "request_router",
            self._dispatch_route,
            {
                "plan": "orchestrate",
                "human_handoff": "render_handoff_template",
                "out_of_scope": "render_out_of_scope_template",
                "unsupported": "render_unsupported_template",
                "security_refusal": "render_security_template",
                "clarification": "render_clarification_template",
                "direct_response": "render_direct_template",
            },
        )
        workflow.add_conditional_edges(
            "orchestrate",
            self._dispatch_decision,
            {
                "execute": "execute_tool_wave",
                "guard": "terminal_guard",
            },
        )
        workflow.add_edge("execute_tool_wave", "normalize_tool_results")
        workflow.add_edge("normalize_tool_results", "update_subquery_ledger")
        workflow.add_edge("update_subquery_ledger", "orchestrate")
        workflow.add_conditional_edges(
            "terminal_guard",
            self._dispatch_terminal_guard,
            {
                "replan": "orchestrate",
                "respond": "finalize_response",
                "handoff": "render_handoff_template",
                "out_of_scope": "render_out_of_scope_template",
            },
        )
        workflow.add_edge("finalize_response", "persist_turn")
        workflow.add_edge("render_handoff_template", "persist_turn")
        workflow.add_edge("render_out_of_scope_template", "persist_turn")
        workflow.add_edge("render_unsupported_template", "persist_turn")
        workflow.add_edge("render_security_template", "persist_turn")
        workflow.add_edge("render_clarification_template", "persist_turn")
        workflow.add_edge("render_direct_template", "persist_turn")
        workflow.add_edge("persist_turn", END)
        return workflow.compile()

    async def _load_context(self, state: AgentState) -> AgentState:
        prepared = await self.context_service.prepare_turn(
            state["user_id"], state.get("conversation_id"), state["message"]
        )
        state["prepared_turn"] = prepared
        state["conversation_id"] = prepared.conversation_id
        state["user_message_id"] = prepared.user_message_id
        state["run_id"] = prepared.run_id
        state["history"] = [
            item.model_dump(mode="json") for item in prepared.history
        ]
        state["working_memory"] = prepared.working_memory.model_dump(mode="json")
        state["memory"] = [item.model_dump(mode="json") for item in prepared.memory]
        return state

    async def _request_route(self, state: AgentState) -> AgentState:
        call_count = state.get("request_router_call_count", 0)
        plan = _deterministic_pre_route_plan(state["message"])
        route_source = "deterministic_fast_path"
        if (
            plan is None
            and self.request_router is not None
            and call_count < MAX_REQUEST_ROUTER_CALLS
        ):
            call_count += 1
            try:
                message = await self.request_router.ainvoke(_request_router_messages(state))
                if not isinstance(message, AIMessage):
                    raise TypeError("request router returned a non-AI message")
                plan = route_plan_from_ai_message(message)
                route_source = "request_router_llm"
            except (ValidationError, ValueError, TypeError):
                plan = self._fallback_route_plan(state)
                route_source = "request_router_fallback"
            except Exception:
                plan = self._fallback_route_plan(state)
                route_source = "request_router_fallback"
        elif plan is None:
            plan = self._fallback_route_plan(state)
            route_source = "deterministic_fallback"

        plan = _enforce_route_boundaries(plan, original_message=state["message"])
        state["request_router_call_count"] = call_count
        state["route_source"] = route_source
        state["route_plan"] = plan.model_dump(mode="json")
        state["rewritten_query"] = plan.rewritten_query
        state["planned_subqueries"] = [
            item.model_dump(mode="json") for item in tool_planning_subqueries(plan)
        ]
        state["blocked_subqueries"] = [
            item.model_dump(mode="json") for item in blocked_subqueries(plan)
        ]
        state["boundary"] = _boundary_from_route_plan(plan)
        state["intent"] = _intent_from_route_plan(plan)
        return state

    def _fallback_route_plan(self, state: AgentState) -> RequestRoutePlan:
        rewritten = _fallback_rewritten_query(state, self.memory_service)
        segments = _split_request_subqueries(rewritten)
        subqueries: list[RoutedSubquery] = []
        for index, query in enumerate(segments, start=1):
            disposition = _fallback_route_disposition(
                query,
                state.get("working_memory", {}),
            )
            clarification_question = (
                "你具体想咨询哪款商品、哪笔订单或哪项商城服务？"
                if disposition == "clarification"
                else ""
            )
            subqueries.append(
                RoutedSubquery(
                    id=f"sq_{index}",
                    query=query,
                    disposition=disposition,
                    reason_code=f"fallback_{disposition}",
                    missing_information=(
                        ["具体咨询对象"] if disposition == "clarification" else []
                    ),
                    clarification_question=clarification_question,
                )
            )
        return RequestRoutePlan(rewritten_query=rewritten, subqueries=subqueries)

    def _dispatch_route(self, state: AgentState) -> str:
        plan = RequestRoutePlan.model_validate(state["route_plan"])
        if tool_planning_subqueries(plan):
            return "plan"
        dispositions = {item.disposition for item in plan.subqueries}
        for disposition in (
            "security_refusal",
            "human_handoff",
            "clarification",
            "unsupported",
            "out_of_scope",
            "direct_response",
        ):
            if disposition in dispositions:
                return disposition
        return "clarification"

    async def _orchestrate(self, state: AgentState) -> AgentState:
        call_count = state.get("orchestrator_call_count", 0) + 1
        boundary = classify_boundary(state["message"])
        if not state.get("route_plan") and boundary.classification == "human_handoff_required":
            decision = OrchestratorDecision(
                type="handoff",
                reason=boundary.reason,
                control_action="request_handoff",
            )
        elif not state.get("route_plan") and boundary.classification == "out_of_scope":
            decision = OrchestratorDecision(
                type="out_of_scope",
                reason=boundary.reason,
                control_action="reject_out_of_scope",
            )
        elif call_count > MAX_ORCHESTRATOR_CALLS:
            decision = _state_terminal_decision(state, "orchestration_limit_reached")
        elif self.orchestrator:
            try:
                decision = await self._invoke_orchestrator_decision(state, call_count)
            except (ValidationError, ValueError, TypeError) as exc:
                reason = f"invalid_orchestrator_response:{type(exc).__name__}"
                decision = OrchestratorDecision(type="invalid", reason=reason)
            except Exception as exc:
                if not state.get("tool_results"):
                    raise
                reason = f"orchestrator_unavailable:{type(exc).__name__}"
                decision = _state_terminal_decision(state, reason)
        else:
            decision = self._fallback_orchestrator_decision(state)

        decision = self._validate_decision_budget(state, decision, call_count)
        state["orchestrator_call_count"] = call_count
        state["decision"] = decision.model_dump(mode="json")
        state["intent"] = _tag_from_decision(decision, state.get("intent"))
        if state.get("route_plan"):
            state["boundary"] = _boundary_from_route_plan(
                RequestRoutePlan.model_validate(state["route_plan"])
            )
        else:
            state["boundary"] = _boundary_from_decision(decision)
        return state

    async def _invoke_orchestrator_decision(
        self,
        state: AgentState,
        call_count: int,
    ) -> OrchestratorDecision:
        model = self._orchestrator_model_for_state(state)
        message = await model.ainvoke(
            _orchestrator_messages(state, call_count)
        )
        if not isinstance(message, AIMessage):
            raise TypeError("orchestrator returned a non-AI message")
        try:
            return decision_from_ai_message(message)
        except ValueError:
            recovered = _plain_text_observation_decision(state, message)
            if recovered is not None:
                return recovered
            raise

    def _orchestrator_model_for_state(self, state: AgentState) -> Any:
        if not state.get("route_plan"):
            return self.orchestrator
        if not state.get("tool_waves"):
            return self.tool_planner
        if _planner_requires_business_tools(state):
            return self.recovery_planner
        return self.answer_synthesizer

    def _validate_decision_budget(
        self,
        state: AgentState,
        decision: OrchestratorDecision,
        call_count: int,
    ) -> OrchestratorDecision:
        if decision.type != "tool_calls":
            return decision

        known_tools = {
            contract.name for contract in self.contract_provider.list_contracts()
        }
        if not decision.tool_calls or any(
            call.name not in known_tools for call in decision.tool_calls
        ):
            if state.get("tool_results"):
                return _state_terminal_decision(state, "unknown_or_empty_tool_call")
            return _clarification_decision(
                "我暂时无法安全选择业务工具，请换一种方式描述你的需求。",
                "unknown_or_empty_tool_call",
            )
        if state.get("route_plan") and state.get("tool_wave_count", 0) == 0:
            constrained_calls = _constrain_calls_to_route_plan(state, decision.tool_calls)
            if not constrained_calls:
                routed_subqueries = tool_planning_subqueries(state.get("route_plan"))
                fallback = self._fallback_routed_tool_decision(
                    state,
                    routed_subqueries,
                )
                decision = fallback.model_copy(
                    update={"reason": "tool_planner_subquery_binding_fallback"}
                )
            else:
                decision = decision.model_copy(update={"tool_calls": constrained_calls})
        decision = decision.model_copy(
            update={
                "tool_calls": _unique_tool_call_ids(
                    decision.tool_calls,
                    state.get("tool_waves", []),
                    call_count,
                )
            }
        )
        if state.get("tool_wave_count", 0) >= MAX_TOOL_WAVES or (
            call_count >= MAX_ORCHESTRATOR_CALLS
        ):
            return _state_terminal_decision(state, "orchestration_limit_reached")
        if state.get("tool_wave_count", 0) > 0:
            allowed_calls: list[PlannedToolCall] = []
            for call in decision.tool_calls:
                try:
                    effective_call, _ = self._prepare_tool_call(state, call)
                except ValidationError:
                    continue
                if _followup_tool_call_allowed(state, effective_call):
                    allowed_calls.append(call)
            if not allowed_calls:
                return _state_terminal_decision(state, "unnecessary_or_invalid_followup")
            decision = decision.model_copy(update={"tool_calls": allowed_calls})
        return decision

    def _dispatch_decision(self, state: AgentState) -> str:
        decision_type = state["decision"]["type"]
        if decision_type == "tool_calls":
            return "execute"
        return "guard"

    async def _execute_tool_wave(self, state: AgentState) -> AgentState:
        decision = OrchestratorDecision.model_validate(state["decision"])
        repo = ConversationRepository(self.session)
        writer = get_stream_writer()
        calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []

        for planned_call in decision.tool_calls:
            call = planned_call
            execution: ToolExecutionResult | None = None
            reused_from_tool_call_id: str | None = None
            try:
                call, applied_memory_ids = self._prepare_tool_call(state, planned_call)
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
                contract = self.contract_provider.get_contract(call.name)
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
                        execution = await self.tool_executor.execute(
                            contract,
                            call.arguments,
                            {"user_id": state["user_id"]},
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
            _apply_tool_output(state, call, execution)

        wave = {
            "wave": state.get("tool_wave_count", 0) + 1,
            "calls": calls,
            "results": results,
        }
        state.setdefault("tool_waves", []).append(wave)
        state.setdefault("tool_results", []).extend(results)
        state["tool_wave_count"] = wave["wave"]
        return state

    async def _normalize_tool_results(self, state: AgentState) -> AgentState:
        state["normalized_tool_results"] = [
            normalize_tool_result(result).model_dump(mode="json")
            for result in state.get("tool_results", [])
        ]
        return state

    async def _update_subquery_ledger(self, state: AgentState) -> AgentState:
        state["subquery_ledger"] = [
            entry.model_dump(mode="json")
            for entry in build_subquery_ledger(state.get("tool_waves", []))
        ]
        _rebuild_tool_projections(state)
        return state

    async def _terminal_guard(self, state: AgentState) -> AgentState:
        decision = OrchestratorDecision.model_validate(state["decision"])
        validation = validate_terminal_decision(
            decision,
            state.get("subquery_ledger", []),
            allow_direct=_is_safe_direct_request(state["message"]),
        )
        if validation.valid:
            if decision.control_action in {"finish_answer", "finish_partial"}:
                used_ids = set(decision.used_tool_call_ids)
                for entry in state.get("subquery_ledger", []):
                    if str(entry.get("tool_call_id") or "") in used_ids:
                        entry["status"] = "answered"
            if decision.control_action == "finish_unavailable":
                decision.response = _fallback_unavailable_answer(state)
                state["decision"] = decision.model_dump(mode="json")
            state["terminal_guard_status"] = "accepted"
            state.pop("terminal_guard_feedback", None)
            return state

        if _usable_tool_call_ids(state):
            fallback = _terminal_fallback_decision(state, validation.reason)
            state["decision"] = fallback.model_dump(mode="json")
            state["intent"] = _tag_from_decision(fallback, state.get("intent"))
            state["boundary"] = _boundary_for_state_decision(state, fallback)
            state["terminal_guard_status"] = "fallback"
            return state

        can_replan = (
            state.get("terminal_guard_replan_count", 0) < 1
            and state.get("orchestrator_call_count", 0) < MAX_ORCHESTRATOR_CALLS
            and self.orchestrator is not None
        )
        if can_replan:
            state["terminal_guard_replan_count"] = (
                state.get("terminal_guard_replan_count", 0) + 1
            )
            state["terminal_guard_feedback"] = validation.reason
            state["terminal_guard_status"] = "replan"
            return state

        fallback = _terminal_fallback_decision(state, validation.reason)
        state["decision"] = fallback.model_dump(mode="json")
        state["intent"] = _tag_from_decision(fallback, state.get("intent"))
        state["boundary"] = _boundary_for_state_decision(state, fallback)
        state["terminal_guard_status"] = "fallback"
        return state

    def _dispatch_terminal_guard(self, state: AgentState) -> str:
        if state.get("terminal_guard_status") == "replan":
            return "replan"
        decision_type = state["decision"]["type"]
        if decision_type == "handoff":
            return "handoff"
        if decision_type == "out_of_scope":
            return "out_of_scope"
        return "respond"

    def _prepare_tool_call(
        self,
        state: AgentState,
        call: PlannedToolCall,
    ) -> tuple[PlannedToolCall, list[int]]:
        arguments = dict(call.arguments)
        routed_query = _routed_query_for_call(state, call)
        if routed_query is not None:
            arguments["query"] = routed_query
        effective_message = routed_query or state["message"]

        if call.name == "catalog_compare":
            request = CatalogCompareInput.model_validate(arguments)
            resolved_sku_ids = _resolve_compare_sku_ids(
                state["message"], state.get("working_memory", {})
            )
            if resolved_sku_ids:
                request = request.model_copy(update={"sku_ids": resolved_sku_ids})
            arguments = request.model_dump(mode="json", exclude_none=True)

        elif call.name == "order_lookup":
            arguments.setdefault("query", effective_message)
            arguments["order_id"] = _resolve_order_id(
                effective_message,
                arguments.get("order_id"),
                state.get("working_memory", {}),
                self.memory_service,
            )

        elif (
            not state.get("route_plan")
            and call.name in {"policy_search", "knowledge_search"}
            and _is_v2_policy_followup(state["message"], state.get("working_memory", {}))
        ):
            arguments["query"] = self.memory_service.resolve_knowledge_query(
                state["message"],
                _knowledge_memory_view(state.get("working_memory", {})),
            )

        contract = self.contract_provider.get_contract(call.name)
        if contract is None:
            return call.model_copy(update={"arguments": arguments}), []
        public_input = contract.public_input_model.model_validate(arguments)
        return (
            call.model_copy(
                update={
                    "arguments": public_input.model_dump(
                        mode="json",
                        exclude_none=True,
                    )
                }
            ),
            [],
        )

    async def _finalize_response(self, state: AgentState) -> AgentState:
        decision = OrchestratorDecision.model_validate(state["decision"])
        answer = decision.response.strip() or _fallback_answer(state)
        state["answer"] = _append_blocked_route_notices(answer, state)
        state["suggested_actions"] = _suggest_actions(state)
        return state

    async def _render_handoff_template(self, state: AgentState) -> AgentState:
        boundary = boundary_for_classification("human_handoff_required")
        state["boundary"] = boundary.model_dump(mode="json")
        state["answer"] = _route_terminal_answer(state, "human_handoff")
        state["suggested_actions"] = _suggest_actions(state)
        return state

    async def _render_out_of_scope_template(self, state: AgentState) -> AgentState:
        boundary = boundary_for_classification("out_of_scope")
        state["boundary"] = boundary.model_dump(mode="json")
        state["answer"] = _route_terminal_answer(state, "out_of_scope")
        state["suggested_actions"] = _suggest_actions(state)
        return state

    async def _render_unsupported_template(self, state: AgentState) -> AgentState:
        boundary = boundary_for_classification("unsupported")
        state["boundary"] = boundary.model_dump(mode="json")
        state["answer"] = _route_terminal_answer(state, "unsupported")
        state["suggested_actions"] = _suggest_actions(state)
        return state

    async def _render_security_template(self, state: AgentState) -> AgentState:
        boundary = boundary_for_classification("security_refusal")
        state["boundary"] = boundary.model_dump(mode="json")
        state["answer"] = _route_terminal_answer(state, "security_refusal")
        state["suggested_actions"] = _suggest_actions(state)
        return state

    async def _render_clarification_template(self, state: AgentState) -> AgentState:
        state["boundary"] = boundary_for_classification("in_scope_auto").model_dump(
            mode="json"
        )
        state["answer"] = _route_terminal_answer(state, "clarification")
        state["suggested_actions"] = []
        return state

    async def _render_direct_template(self, state: AgentState) -> AgentState:
        state["boundary"] = boundary_for_classification("in_scope_auto").model_dump(
            mode="json"
        )
        state["answer"] = _route_terminal_answer(state, "direct_response")
        state["suggested_actions"] = _suggest_actions(state)
        return state

    async def _persist_turn(self, state: AgentState) -> AgentState:
        state["assistant_metadata"] = {
            "intent": state["intent"],
            "route_source": state.get("route_source"),
            "decision": state.get(
                "decision",
                {"type": "request_route_terminal", "route_plan": state.get("route_plan")},
            ),
            "boundary": state["boundary"],
            "route_plan": state.get("route_plan"),
            "subquery_ledger": state.get("subquery_ledger", []),
            "evidence": _dump_evidence(state.get("evidence", [])),
            "products": [
                product.model_dump(mode="json") for product in state.get("products", [])
            ],
            "order": (
                state["order"].model_dump(mode="json") if state.get("order") else None
            ),
        }
        outcome = {
            key: value
            for key, value in state.items()
            if key not in {"prepared_turn", "working_memory"}
        }
        changes = await self.context_service.complete_turn(state["prepared_turn"], outcome)
        state["working_memory"] = changes.working_memory.model_dump(mode="json")
        if changes.memory_changes:
            state["memory_changes"] = [
                item.model_dump(mode="json") for item in changes.memory_changes
            ]
        return state

    def _fallback_orchestrator_decision(self, state: AgentState) -> OrchestratorDecision:
        if state.get("tool_results"):
            order_output = _latest_successful_tool_output(state, "order_lookup")
            order_candidates = (
                order_output.get("candidates")
                if isinstance(order_output, dict)
                else None
            )
            if (
                not state.get("order")
                and state.get("tool_wave_count", 0) < MAX_TOOL_WAVES
                and isinstance(order_candidates, list)
                and order_candidates
                and isinstance(order_candidates[0], dict)
                and order_candidates[0].get("id") is not None
            ):
                return _tool_decision(
                    "order_lookup",
                    {"order_id": order_candidates[0]["id"], "limit": 1},
                )
            usable_ids = _usable_tool_call_ids(state)
            if usable_ids:
                return OrchestratorDecision(
                    type="grounded_response",
                    response=_fallback_answer(state),
                    reason="llm_not_configured",
                    control_action="finish_answer",
                    used_tool_call_ids=usable_ids,
                )
            return OrchestratorDecision(
                type="unavailable_response",
                response=_fallback_unavailable_answer(state),
                reason="llm_not_configured",
                control_action="finish_unavailable",
                unavailable_parts=["请求所需的业务信息"],
            )

        routed_subqueries = tool_planning_subqueries(state.get("route_plan"))
        if routed_subqueries:
            return self._fallback_routed_tool_decision(state, routed_subqueries)

        boundary = classify_boundary(state["message"])
        if boundary.classification == "human_handoff_required":
            return OrchestratorDecision(
                type="handoff",
                reason=boundary.reason,
                control_action="request_handoff",
            )
        if boundary.classification == "out_of_scope":
            return OrchestratorDecision(
                type="out_of_scope",
                reason=boundary.reason,
                control_action="reject_out_of_scope",
            )
        if _is_identity_or_capability_question(state["message"]):
            return OrchestratorDecision(
                type="direct_response",
                response=(
                    "我是 PC 外设商城客服 AI，可以帮你推荐和对比外设、查询订单物流，"
                    "以及说明售后政策和选购知识。"
                ),
                reason="identity_or_capability_question",
                control_action="finish_direct",
            )

        if facet_arguments := _fallback_catalog_facets_arguments(state["message"]):
            return _tool_decision("catalog_facets", facet_arguments)

        intent = _contextual_intent(
            state["message"],
            state.get("working_memory", {}),
            self.memory_service,
        )
        if intent == "product_recommendation":
            compare_sku_ids = _resolve_compare_sku_ids(
                state["message"], state.get("working_memory", {})
            )
            if compare_sku_ids:
                return _tool_decision(
                    "catalog_compare",
                    {
                        "query": state["message"],
                        "sku_ids": compare_sku_ids,
                        "limit": 5,
                    },
                )
            return _tool_decision(
                "catalog_search",
                {
                    "query": _fallback_catalog_query(state),
                    "limit": 3,
                },
            )
        if intent == "order_status":
            return _tool_decision(
                "order_lookup",
                {
                    "query": state["message"],
                    "order_id": _resolve_order_id(
                        state["message"],
                        extract_order_id(state["message"]),
                        state.get("working_memory", {}),
                        self.memory_service,
                    ),
                    "limit": 1,
                },
            )
        if intent == "after_sales":
            return _tool_decision(
                "policy_search",
                {
                    "query": self.memory_service.resolve_knowledge_query(
                        state["message"],
                        _knowledge_memory_view(state.get("working_memory", {})),
                    ),
                    "limit": 3,
                    "retrieval_mode": "hybrid",
                },
            )
        if intent == "purchase_guidance":
            return OrchestratorDecision(
                type="direct_response",
                response=_purchase_guidance_answer(),
                reason="read_only_purchase_guidance",
                control_action="finish_direct",
            )
        return _tool_decision(
            "knowledge_search",
            {"query": state["message"], "limit": 3, "retrieval_mode": "hybrid"},
        )

    def _fallback_routed_tool_decision(
        self,
        state: AgentState,
        subqueries: list[RoutedSubquery],
    ) -> OrchestratorDecision:
        calls: list[PlannedToolCall] = []
        for subquery in subqueries:
            query = subquery.query
            facet_arguments = _fallback_catalog_facets_arguments(query)
            if facet_arguments:
                name = "catalog_facets"
                arguments = facet_arguments
            else:
                intent = classify_intent(query)
                if intent == "product_recommendation":
                    compare_sku_ids = _resolve_compare_sku_ids(
                        state["message"], state.get("working_memory", {})
                    )
                    if compare_sku_ids:
                        name = "catalog_compare"
                        arguments = {
                            "query": query,
                            "sku_ids": compare_sku_ids,
                            "limit": 5,
                        }
                    else:
                        name = "catalog_search"
                        arguments = {"query": query, "limit": 3}
                elif intent == "order_status":
                    name = "order_lookup"
                    arguments = {
                        "query": query,
                        "order_id": _resolve_order_id(
                            query,
                            extract_order_id(query),
                            state.get("working_memory", {}),
                            self.memory_service,
                        ),
                        "limit": 1,
                    }
                elif intent == "after_sales":
                    name = "policy_search"
                    arguments = {
                        "query": query,
                        "limit": 3,
                        "retrieval_mode": "hybrid",
                    }
                else:
                    name = "knowledge_search"
                    arguments = {
                        "query": query,
                        "limit": 3,
                        "retrieval_mode": "hybrid",
                    }
            calls.append(
                PlannedToolCall(
                    id=f"fallback_{subquery.id}_{name}",
                    name=name,
                    arguments=arguments,
                    subquery=subquery.id,
                )
            )
        return OrchestratorDecision(type="tool_calls", tool_calls=calls)

    async def _mark_run_failed(
        self, state: AgentState, error_type: str, message: str
    ) -> None:
        if not state.get("run_id"):
            return
        prepared = state.get("prepared_turn")
        if prepared is not None:
            audit = serialize_compact_audit(
                {
                    key: value
                    for key, value in state.items()
                    if key not in {"prepared_turn", "working_memory"}
                },
                estimated_token_count=prepared.estimated_token_count,
                retained_turns=prepared.retained_turns,
                dropped_turns=prepared.dropped_turns,
                applied_memory_ids=state.get("applied_memory_ids", []),
            )
        else:
            audit = {
                key: value
                for key, value in _json_safe_state(state).items()
                if key not in {"history", "memory", "working_memory", "prepared_turn"}
            }
        repo = ConversationRepository(self.session)
        await repo.fail_run(
            state["run_id"],
            state.get("intent"),
            audit,
            {"type": error_type, "message": message},
        )
        await self.session.commit()

    async def _mark_stream_failed(
        self, state: AgentState, error_type: str, message: str
    ) -> None:
        """Compatibility entry point for callers that finalize failed SSE runs."""
        await self._mark_run_failed(state, error_type, message)

    def _response_from_state(self, state: AgentState) -> ChatResponse:
        return ChatResponse(
            conversation_id=state["conversation_id"],
            answer=state["answer"],
            intent=state.get("intent", "general"),
            boundary=BoundaryClassification(**state["boundary"]),
            evidence=state.get("evidence", []),
            products=state.get("products", []),
            order=state.get("order"),
            suggested_actions=[
                SuggestedAction(**item) for item in state.get("suggested_actions", [])
            ],
            memory_changes=state.get("memory_changes"),
        )


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


def _legacy_memory_view(working_memory: dict[str, Any]) -> dict[str, Any]:
    order = working_memory.get("order")
    if not isinstance(order, dict) or order.get("last_order_id") is None:
        return working_memory
    return {**working_memory, "last_order_id": order["last_order_id"]}


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
    if requires_security_refusal(query):
        return "security_refusal"
    if requires_static_unsupported(query):
        return "unsupported"
    boundary = classify_boundary(query)
    if boundary.classification == "human_handoff_required":
        return "human_handoff"
    if boundary.classification == "out_of_scope":
        return "out_of_scope"
    if boundary.classification == "security_refusal":
        return "security_refusal"
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
    unsupported_markers = (
        "历史价格",
        "价格预测",
        "未来价格",
        "销量趋势",
        "销量增长率",
        "销量环比",
        "图片故障诊断",
        "自动检测兼容性",
    )
    if any(marker in compact for marker in unsupported_markers):
        return "unsupported"
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
        enforced.append(
            item.model_copy(
                update={
                    "query": " ".join(item.query.split()),
                    "disposition": disposition,
                    "reason_code": reason_code,
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
    return plan.model_copy(
        update={
            "rewritten_query": " ".join(plan.rewritten_query.split()),
            "subqueries": enforced,
        }
    )


def _hard_route_boundary(query: str) -> tuple[str, str] | None:
    if requires_security_refusal(query):
        return "security_refusal", "runtime_security_guard"
    if requires_static_unsupported(query):
        return "unsupported", "runtime_static_capability_guard"
    boundary = classify_boundary(query)
    if boundary.classification == "human_handoff_required":
        return "human_handoff", "runtime_handoff_guard"
    if boundary.classification == "out_of_scope":
        return "out_of_scope", "runtime_scope_guard"
    return None


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
        return boundary_for_classification(
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
    return boundary_for_classification(classification).model_dump(mode="json")


def _boundary_for_state_decision(
    state: AgentState,
    decision: OrchestratorDecision,
) -> dict[str, Any]:
    if state.get("route_plan"):
        return _boundary_from_route_plan(
            RequestRoutePlan.model_validate(state["route_plan"])
        )
    return _boundary_from_decision(decision)


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
    route_plan = state.get("route_plan")
    if not route_plan:
        for item in state.get("history", []):
            content = item.get("content", "")
            if not content:
                continue
            if item.get("role") == "user":
                messages.append(HumanMessage(content=content))
            elif item.get("role") == "assistant":
                messages.append(AIMessage(content=content))
    routed_subqueries = [
        {"id": item.id, "canonical_query": item.query}
        for item in tool_planning_subqueries(route_plan)
    ]
    planner_request = (
        None
        if route_plan
        else str(state.get("rewritten_query") or state["message"])
    )
    messages.append(
        HumanMessage(
            content=build_orchestrator_user_prompt(
                message=planner_request,
                tool_wave_count=state.get("tool_wave_count", 0),
                orchestrator_call_count=call_count,
                memory_context=(
                    None
                    if route_plan
                    else {
                        "priority": (
                            "current request and tool results > working memory > "
                            "explicit user preferences > recent history"
                        ),
                        "working_memory": state.get("working_memory", {}),
                        "explicit_user_preferences": state.get("memory", []),
                    }
                ),
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


def _boundary_from_decision(decision: OrchestratorDecision) -> dict[str, Any]:
    if decision.type == "handoff":
        classification = "human_handoff_required"
    elif decision.type == "out_of_scope":
        classification = "out_of_scope"
    else:
        classification = "in_scope_auto"
    return boundary_for_classification(
        classification,
        reason=decision.reason or None,
    ).model_dump(mode="json")


def _tag_from_decision(
    decision: OrchestratorDecision,
    current_tag: str | None,
) -> str:
    if decision.type == "handoff":
        return current_tag or "human_handoff_required"
    if decision.type == "out_of_scope":
        return "out_of_scope"
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


def _apply_tool_output(
    state: AgentState,
    call: PlannedToolCall,
    execution: ToolExecutionResult,
) -> None:
    if call.name in {"catalog_search", "catalog_compare"}:
        state["catalog_tool_succeeded"] = execution.ok
    if not execution.ok or not execution.output:
        return
    output = execution.output
    if call.name in {"catalog_search", "catalog_compare"}:
        state["products"] = [
            ProductCard.model_validate(product) for product in output.get("products", [])
        ]
        if call.name == "catalog_search":
            state.setdefault("parsed", {})["product_search"] = output.get(
                "query_plan", {}
            )
        else:
            state.setdefault("parsed", {})["catalog_comparison"] = {
                "query": call.arguments.get("query"),
                "sku_ids": [product.sku_id for product in state["products"]],
                "comparison_fields": output.get("comparison_fields", []),
            }
    elif call.name in {"policy_search", "knowledge_search"}:
        evidence = [
            EvidenceItem.model_validate(document) for document in output.get("documents", [])
        ]
        state["evidence"] = _dedupe_evidence([*state.get("evidence", []), *evidence])
    elif call.name == "order_lookup":
        if output.get("order"):
            state["order"] = OrderCard.model_validate(output["order"])
        state.setdefault("parsed", {})["order_candidates"] = output.get("candidates", [])


def _rebuild_tool_projections(state: AgentState) -> None:
    """Rebuild compatibility state from active observations instead of the latest call."""
    ledger = state.get("subquery_ledger", [])
    active_ids = {
        str(entry.get("tool_call_id"))
        for entry in ledger
        if is_active_ledger_entry(entry) and entry.get("tool_call_id")
    }
    if not active_ids:
        return

    products: list[ProductCard] = []
    seen_sku_ids: set[int] = set()
    evidence: list[EvidenceItem] = []
    order: OrderCard | None = None
    order_candidates: list[dict[str, Any]] = []
    parsed = state.setdefault("parsed", {})
    parsed.pop("product_search", None)
    parsed.pop("catalog_comparison", None)
    parsed.pop("order_candidates", None)
    saw_catalog_result = False
    catalog_completed = False

    for result in state.get("tool_results", []):
        call_id = str(result.get("tool_call_id") or "")
        if call_id not in active_ids:
            continue
        name = str(result.get("name") or "")
        execution = result.get("execution", {})
        if not isinstance(execution, dict):
            continue
        output = execution.get("output")
        if name in {"catalog_search", "catalog_compare"}:
            saw_catalog_result = True
            catalog_completed = catalog_completed or bool(execution.get("ok"))
        if not execution.get("ok") or not isinstance(output, dict):
            continue

        if name in {"catalog_search", "catalog_compare"}:
            for item in output.get("products", []):
                product = ProductCard.model_validate(item)
                if product.sku_id in seen_sku_ids:
                    continue
                seen_sku_ids.add(product.sku_id)
                products.append(product)
            if name == "catalog_search":
                parsed["product_search"] = output.get("query_plan", {})
            else:
                parsed["catalog_comparison"] = {
                    "query": _tool_call_arguments(state, call_id).get("query"),
                    "sku_ids": [item.get("sku_id") for item in output.get("products", [])],
                    "comparison_fields": output.get("comparison_fields", []),
                }
        elif name in {"policy_search", "knowledge_search"}:
            evidence.extend(
                EvidenceItem.model_validate(item)
                for item in output.get("documents", [])
            )
        elif name == "order_lookup":
            if output.get("order"):
                order = OrderCard.model_validate(output["order"])
            candidates = output.get("candidates")
            if isinstance(candidates, list):
                order_candidates = candidates

    state["products"] = products
    state["evidence"] = _dedupe_evidence(evidence)
    state["order"] = order
    parsed["order_candidates"] = order_candidates
    if saw_catalog_result:
        state["catalog_tool_succeeded"] = catalog_completed


def _tool_call_arguments(state: AgentState, call_id: str) -> dict[str, Any]:
    for wave in state.get("tool_waves", []):
        for call in wave.get("calls", []):
            if str(call.get("id") or "") == call_id:
                arguments = call.get("arguments")
                return arguments if isinstance(arguments, dict) else {}
    return {}


def _fallback_answer(state: AgentState) -> str:
    products = state.get("products", [])
    if products:
        product_search = state.get("parsed", {}).get("product_search", {})
        usage_mapping = (
            product_search.get("usage_mapping", {})
            if isinstance(product_search, dict)
            else {}
        )
        usage_status = (
            str(usage_mapping.get("status") or "")
            if isinstance(usage_mapping, dict)
            else ""
        )
        if usage_status == "applied":
            intro = "我根据这个使用场景相关的规格要求和偏好，找到了这些候选："
        elif usage_status == "expanded":
            intro = "我根据这个使用场景，从多个相关外设品类中找到了这些候选："
        else:
            intro = "我根据商品目录找到了这些候选："
        lines = [intro]
        asks_sales = any(
            term in state.get("message", "").lower()
            for term in ("销量", "热销", "畅销", "sales")
        )
        for product in products[:3]:
            customer_specs = [
                (CUSTOMER_SPEC_LABELS[key.casefold()], value)
                for key, value in product.specs.items()
                if key.casefold() in CUSTOMER_SPEC_LABELS
            ][:4]
            specs = "，".join(f"{label}: {value}" for label, value in customer_specs)
            suffix = f"，{specs}" if specs else ""
            if asks_sales:
                suffix += (
                    f"，当前版本销量 {product.sku_sales_count}，"
                    f"整个商品系列累计销量 {product.sales_count}"
                )
            lines.append(
                f"- {product.title}：¥{product.price}，库存 {product.stock}{suffix}。"
            )
        if usage_status in {"applied", "expanded"}:
            lines.append(
                "如果你想继续缩小范围，可以补充预算或最在意的具体规格；"
                "实际价格和库存以下单页为准。"
            )
        else:
            lines.append(
                "告诉我主要用途后，我可以继续判断哪一款更适合；"
                "实际价格和库存以下单页为准。"
            )
        return "\n".join(lines)

    order = state.get("order")
    if order:
        logistics = order.logistics
        shipping = "暂未查询到物流单号"
        if logistics and logistics.logistic_no:
            shipping = f"{logistics.express_company or '快递'} {logistics.logistic_no}"
        return (
            f"订单 {order.id} 当前状态为「{order.status_label}」，实付 ¥{order.pay_amount}。\n"
            f"物流：{shipping}。"
        )

    candidates = state.get("parsed", {}).get("order_candidates", [])
    if candidates:
        lines = ["我查到这些最近订单，请告诉我你要查看哪一单："]
        lines.extend(
            f"- 订单 {item['id']}：{item['status_label']}，实付 ¥{item['pay_amount']}。"
            for item in candidates[:5]
        )
        return "\n".join(lines)

    evidence = state.get("evidence", [])
    if evidence:
        if len(evidence) > 1:
            titles = "、".join(item.title for item in evidence[:3])
            return (
                f"我找到了可能相关的资料（{titles}），但暂时无法可靠归纳出直接答案。"
                "请稍后重试，或把问题描述得更具体一些。"
            )
        lines = ["我根据知识库查到以下信息："]
        lines.extend(f"- {item.title}：{item.snippet}" for item in evidence)
        lines.append("依据：" + "、".join(item.title for item in evidence))
        return "\n".join(lines)

    facet_output = _latest_successful_tool_output(state, "catalog_facets")
    if facet_output is not None:
        return _catalog_facets_fallback_answer(facet_output)

    failed_results = [
        result
        for result in _active_tool_results(state)
        if not result.get("execution", {}).get("ok")
    ]
    if failed_results:
        return "业务信息查询暂时失败，请稍后重试或补充更具体的信息。"
    return "我暂时没有找到足够的信息，请补充具体商品、订单号或想咨询的政策。"


def _fallback_catalog_facets_arguments(message: str) -> dict[str, Any] | None:
    compact = message.lower().replace(" ", "")
    list_markers = ("哪些", "有什么", "有啥", "都有什么", "都有哪些", "可选", "提供")
    asks_for_list = any(marker in compact for marker in list_markers)
    if not asks_for_list:
        return None

    if any(term in compact for term in ("品牌", "牌子")):
        return {"query": message, "limit": 20}
    if any(term in compact for term in ("品类", "类目", "商品类型", "外设类型")):
        return {"query": message, "limit": 20}
    if any(
        term in compact
        for term in ("轴体", "刷新率", "分辨率", "连接方式", "dpi", "颜色")
    ):
        return {"query": message, "limit": 20}
    return None


def _latest_successful_tool_output(
    state: AgentState,
    tool_name: str,
) -> dict[str, Any] | None:
    for result in reversed(_active_tool_results(state)):
        execution = result.get("execution", {})
        if result.get("name") == tool_name and execution.get("ok"):
            output = execution.get("output")
            if isinstance(output, dict):
                return output
    return None


def _active_tool_results(state: AgentState) -> list[dict[str, Any]]:
    ledger = state.get("subquery_ledger", [])
    if not ledger:
        return state.get("tool_results", [])
    active_ids = {
        str(entry.get("tool_call_id"))
        for entry in ledger
        if is_active_ledger_entry(entry) and entry.get("tool_call_id")
    }
    return [
        result
        for result in state.get("tool_results", [])
        if str(result.get("tool_call_id") or "") in active_ids
    ]


def _has_successful_tool_result(state: AgentState) -> bool:
    return bool(_usable_tool_call_ids(state))


def _usable_tool_call_ids(state: AgentState) -> list[str]:
    ledger = state.get("subquery_ledger", [])
    if ledger:
        return active_usable_tool_call_ids(ledger)
    return [
        outcome.tool_call_id
        for outcome in (
            normalize_tool_result(result) for result in state.get("tool_results", [])
        )
        if outcome.has_usable_information and outcome.tool_call_id
    ]


def _fallback_unavailable_answer(state: AgentState) -> str:
    outcomes = [
        normalize_tool_result(result) for result in _active_tool_results(state)
    ]
    kinds = {outcome.outcome for outcome in outcomes}
    tool_names = {outcome.tool_name for outcome in outcomes}
    catalog_output = _latest_successful_tool_output(state, "catalog_search")
    if catalog_output and _has_catalog_diagnostic(
        catalog_output, "usage_mapping_unavailable"
    ):
        return (
            "目前商品数据缺少能够可靠判断这个使用场景的规格依据，我不能仅凭商品名称推荐。"
            "你可以补充预算、连接方式、重量或最在意的具体规格，我再继续筛选。"
        )
    if "unsupported" in kinds:
        return (
            "这个请求属于 PC 外设相关，但当前商品查询能力不支持这类信息。"
            "你可以改问具体商品、价格、库存、规格或目录筛选。"
        )
    if "error" in kinds:
        return "业务信息查询暂时失败，请稍后重试。"
    if "not_found" in kinds and tool_names == {"order_lookup"}:
        return "当前账号下没有找到对应订单。请核对订单号，或让我查询最近订单。"
    if "insufficient" in kinds and "catalog_compare" in tool_names:
        return "当前只找到不足两款可比商品，暂时无法完成有效对比。请补充具体型号。"
    if tool_names <= {"policy_search", "knowledge_search"}:
        return "当前知识库没有找到足够依据，我不能凭模型常识补写答案。"
    if tool_names <= {"catalog_search", "catalog_compare", "catalog_facets"}:
        return "当前商品目录没有找到匹配信息。你可以补充型号，或放宽一个筛选条件。"
    return "本次查询没有得到可用于回答的信息，请补充更具体的商品、订单或政策问题。"


def _has_catalog_diagnostic(output: dict[str, Any], code: str) -> bool:
    diagnostics = output.get("diagnostics")
    if not isinstance(diagnostics, list):
        return False
    return any(
        isinstance(diagnostic, dict) and diagnostic.get("code") == code
        for diagnostic in diagnostics
    )


def _catalog_facets_fallback_answer(output: dict[str, Any]) -> str:
    facet_labels = {
        "brand": "品牌",
        "category": "类目",
        "spec_key": "规格字段",
        "spec_value": "规格选项",
    }
    label = facet_labels.get(str(output.get("facet")), "目录选项")
    items = output.get("items")
    if not isinstance(items, list) or not items:
        return f"当前目录中没有找到符合条件的{label}。"

    rendered: list[str] = []
    for item in items[:10]:
        if not isinstance(item, dict) or not item.get("value"):
            continue
        count = item.get("count")
        suffix = f"（{count} 条 SKU 记录）" if isinstance(count, int) else ""
        rendered.append(f"{item['value']}{suffix}")
    if not rendered:
        return f"当前目录中没有找到符合条件的{label}。"
    return f"当前目录中可查到的{label}包括：" + "、".join(rendered) + "。"


def _purchase_guidance_answer() -> str:
    return (
        "下单流程可以按这几步走：\n"
        "1. 在商品页确认 SKU、价格、库存和关键规格。\n"
        "2. 选择规格并确认收货信息。\n"
        "3. 提交订单后按页面提示支付，再到订单页查看状态和物流。\n"
        "我不能在聊天中替你提交订单或完成支付。"
    )


def _store_philosophy_answer() -> str:
    return (
        "我们的服务理念是：围绕 PC 外设，用清晰、克制、有依据的信息帮助你做选择。"
        "商品建议尽量结合真实价格、库存、销量和规格；订单与政策问题尊重隐私和权限，"
        "需要实际执行或人工确认的事项不会替你擅自承诺。"
    )


def _is_store_philosophy_question(message: str) -> bool:
    compact = re.sub(r"\s+", "", message.casefold())
    return any(term in compact for term in ("理念", "使命", "价值观", "宗旨"))


def _is_identity_or_capability_question(message: str) -> bool:
    compact = message.lower().replace(" ", "")
    return any(
        term in compact
        for term in ["你是谁", "你是什么", "你能做什么", "你会什么", "怎么用你"]
    )


def _is_safe_direct_request(message: str) -> bool:
    if _is_identity_or_capability_question(message):
        return True
    if classify_intent(message) == "purchase_guidance":
        return True
    compact = re.sub(r"[\s，。！？!?、,.]", "", message.lower())
    return compact in {
        "你好",
        "您好",
        "hello",
        "hi",
        "谢谢",
        "谢谢你",
        "再见",
    }


def _route_notice(subquery: RoutedSubquery) -> str:
    if subquery.disposition == "clarification":
        return subquery.clarification_question.strip()
    if subquery.disposition == "direct_response":
        if _is_store_philosophy_question(subquery.query):
            return _store_philosophy_answer()
        if classify_intent(subquery.query) == "purchase_guidance":
            return _purchase_guidance_answer()
        if _is_identity_or_capability_question(subquery.query):
            return (
                "我是 PC 外设商城客服 AI，可以帮你推荐和对比外设、查询当前账号的订单物流，"
                "以及说明商城政策和外设选购知识。"
            )
        if any(term in subquery.query.casefold() for term in ("谢谢", "thank")):
            return "不客气，有外设、订单物流或商城政策问题都可以继续问我。"
        if any(term in subquery.query.casefold() for term in ("再见", "bye")):
            return "再见，有外设、订单物流或商城政策问题时欢迎再来。"
        return "你好，我可以帮你处理 PC 外设推荐、本人订单物流和商城政策咨询。"
    classification_by_disposition = {
        "human_handoff": "human_handoff_required",
        "out_of_scope": "out_of_scope",
        "unsupported": "unsupported",
        "security_refusal": "security_refusal",
    }
    classification = classification_by_disposition.get(subquery.disposition)
    if classification is None:
        return ""
    return boundary_for_classification(classification).display_message


def _route_terminal_answer(state: AgentState, disposition: str) -> str:
    route_plan = state.get("route_plan")
    if route_plan:
        plan = RequestRoutePlan.model_validate(route_plan)
        notices = [
            _route_notice(item)
            for item in plan.subqueries
            if item.disposition != "tool_planning"
        ]
        unique = [notice for notice in dict.fromkeys(notices) if notice]
        if unique:
            return "\n\n".join(unique)
    classification_by_disposition = {
        "human_handoff": "human_handoff_required",
        "out_of_scope": "out_of_scope",
        "unsupported": "unsupported",
        "security_refusal": "security_refusal",
    }
    classification = classification_by_disposition.get(disposition)
    if classification is not None:
        return boundary_for_classification(classification).display_message
    if disposition == "clarification":
        return "你具体想咨询哪款商品、哪笔订单或哪项商城服务？"
    return "你好，我可以帮你处理 PC 外设推荐、本人订单物流和商城政策咨询。"


def _append_blocked_route_notices(answer: str, state: AgentState) -> str:
    blocked = blocked_subqueries(state.get("route_plan"))
    if not blocked:
        return answer
    notices = [_route_notice(item) for item in blocked]
    unique = [notice for notice in dict.fromkeys(notices) if notice and notice not in answer]
    if not unique:
        return answer
    return f"{answer.rstrip()}\n\n另外：\n" + "\n".join(f"- {notice}" for notice in unique)


def _suggest_actions(state: AgentState) -> list[dict[str, Any]]:
    boundary = state["boundary"]["classification"]
    if boundary == "human_handoff_required":
        return [{"label": "转人工客服", "payload": _handoff_payload(state["message"])}]
    if boundary in {"out_of_scope", "unsupported", "security_refusal"}:
        return [{"label": "咨询外设推荐", "payload": {"message": "推荐 300 元以内无线鼠标"}}]
    if any(
        item.disposition == "human_handoff"
        for item in blocked_subqueries(state.get("route_plan"))
    ):
        return [{"label": "转人工客服", "payload": _handoff_payload(state["message"])}]
    if state.get("products"):
        return [
            {"label": "查询最近订单", "payload": {"message": "帮我查最近订单"}},
            {"label": "继续筛选", "payload": {"message": "帮我进一步筛选这些商品"}},
        ]
    if state.get("order"):
        return [{"label": "转人工处理售后", "payload": {"orderId": state["order"].id}}]
    return []


def _handoff_payload(message: str) -> dict[str, Any]:
    return {
        "handoff": True,
        "orderId": extract_order_id(message),
        "requestType": _handoff_request_type(message),
        "reason": message,
    }


def _handoff_request_type(message: str) -> str:
    if "退款" in message:
        return "refund"
    if "退货" in message or "换货" in message:
        return "return"
    if "维修" in message or "保修" in message:
        return "repair"
    if any(term in message for term in ["取消订单", "改地址", "改收货", "修改订单"]):
        return "order_change"
    return "other"


def _dedupe_evidence(items: list[EvidenceItem]) -> list[EvidenceItem]:
    by_source: dict[tuple[str, int], EvidenceItem] = {}
    for item in items:
        by_source[(item.source_type, item.source_id)] = item
    return list(by_source.values())


def _dump_evidence(items: list[EvidenceItem]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in items]


def _json_safe_state(state: AgentState) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in state.items():
        if key == "products":
            result[key] = [item.model_dump(mode="json") for item in value]
        elif key == "evidence":
            result[key] = _dump_evidence(value)
        elif key == "order" and value is not None:
            result[key] = value.model_dump(mode="json")
        else:
            result[key] = value
    return result


def _stream_event(event_type: str, state: AgentState, **payload: Any) -> dict[str, Any]:
    event: dict[str, Any] = {"type": event_type}
    if state.get("conversation_id") is not None:
        event["conversation_id"] = state["conversation_id"]
    if state.get("run_id") is not None:
        event["run_id"] = state["run_id"]
    event.update(payload)
    return event


def _context_event(state: AgentState) -> dict[str, Any]:
    return _stream_event(
        "context",
        state,
        intent=state.get("intent"),
        boundary=state.get("boundary"),
        products=[product.model_dump(mode="json") for product in state.get("products", [])],
        order=state["order"].model_dump(mode="json") if state.get("order") else None,
        evidence=_dump_evidence(state.get("evidence", [])),
    )
