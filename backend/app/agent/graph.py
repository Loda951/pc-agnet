import asyncio
import json
import re
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
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
    TerminalResponseStreamParser,
    decision_from_ai_message,
)
from app.agent.intent import (
    boundary_for_classification,
    build_product_search,
    classify_boundary,
    classify_intent,
    extract_order_id,
)
from app.agent.prompts import ORCHESTRATOR_SYSTEM_PROMPT, build_orchestrator_input
from app.agent.state import AgentState
from app.core.config import Settings
from app.core.llm import build_chat_model
from app.repositories.conversations import ConversationRepository
from app.schemas.catalog import ProductCard, ProductSearchRequest
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
    CatalogSearchInput,
    ToolError,
    ToolExecutionResult,
)

MAX_ORCHESTRATOR_CALLS = 3
MAX_TOOL_WAVES = 2


class _RegistryCompatibilityExecutor:
    """Adapt the pre-contract ToolRegistry seam used by existing runtime tests."""

    def __init__(self, registry: Any):
        self.registry = registry

    async def execute(
        self,
        contract: ToolContract,
        arguments: dict[str, Any],
        runtime_context: dict[str, Any],
    ) -> ToolExecutionResult:
        internal_arguments = dict(arguments)
        for field_name in contract.runtime_fields:
            internal_arguments[field_name] = runtime_context[field_name]
        return await self.registry.execute(contract.registry_name, internal_arguments)


class AgentRuntime:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        *,
        contract_provider: ToolContractProvider | None = None,
        tool_executor: ToolExecutor | None = None,
        chat_model: Any | None = None,
        context_service: ConversationContextService | None = None,
        memory_service: MemoryService | None = None,
        tool_registry: Any | None = None,
        knowledge_service: Any | None = None,
    ):
        self.session = session
        self.settings = settings
        self.contract_provider = contract_provider or DefaultToolContractProvider()
        self.tool_executor = (
            tool_executor
            or (
                _RegistryCompatibilityExecutor(tool_registry)
                if tool_registry is not None
                else RegistryToolExecutor(session, settings)
            )
        )
        self.context_service = context_service or ConversationContextService(session, settings)
        self.memory_service = memory_service or MemoryService()
        self.knowledge_service = knowledge_service
        self.llm = chat_model if chat_model is not None else build_chat_model(settings)
        self.orchestrator = (
            self.llm.bind_tools(
                [contract.as_llm_tool() for contract in self.contract_provider.list_contracts()]
            )
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
                    if event_kind == "decision_header":
                        decision = OrchestratorDecision(type=update["decision_type"])
                        yield _stream_event(
                            "boundary",
                            state,
                            boundary=_boundary_from_decision(decision),
                        )
                    elif event_kind == "response_delta":
                        yield _stream_event("delta", state, delta=update["delta"])
                    elif event_kind == "tool_call":
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
                    elif node_name == "orchestrate" and not state.get(
                        "decision_header_streamed"
                    ):
                        yield _stream_event("boundary", state, boundary=state["boundary"])
                    elif node_name == "execute_tool_wave":
                        yield _context_event(state)
                    elif node_name in {
                        "finalize_response",
                        "render_handoff_template",
                        "render_out_of_scope_template",
                    }:
                        if not state.get("response_streamed"):
                            for delta in _chunk_text(state["answer"]):
                                await asyncio.sleep(0)
                                yield _stream_event("delta", state, delta=delta)
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
            "orchestrator_call_count": 0,
            "tool_wave_count": 0,
            "tool_waves": [],
            "tool_results": [],
            "decision_header_streamed": False,
            "response_streamed": False,
            "parsed": {},
            "products": [],
            "evidence": [],
            "order": None,
        }

    def _build_graph(self):
        workflow = StateGraph(AgentState)
        workflow.add_node("load_context", self._load_context)
        workflow.add_node("orchestrate", self._orchestrate)
        workflow.add_node("execute_tool_wave", self._execute_tool_wave)
        workflow.add_node("finalize_response", self._finalize_response)
        workflow.add_node("render_handoff_template", self._render_handoff_template)
        workflow.add_node("render_out_of_scope_template", self._render_out_of_scope_template)
        workflow.add_node("persist_turn", self._persist_turn)

        workflow.set_entry_point("load_context")
        workflow.add_edge("load_context", "orchestrate")
        workflow.add_conditional_edges(
            "orchestrate",
            self._dispatch_decision,
            {
                "execute": "execute_tool_wave",
                "respond": "finalize_response",
                "handoff": "render_handoff_template",
                "out_of_scope": "render_out_of_scope_template",
            },
        )
        workflow.add_edge("execute_tool_wave", "orchestrate")
        workflow.add_edge("finalize_response", "persist_turn")
        workflow.add_edge("render_handoff_template", "persist_turn")
        workflow.add_edge("render_out_of_scope_template", "persist_turn")
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

    async def _orchestrate(self, state: AgentState) -> AgentState:
        call_count = state.get("orchestrator_call_count", 0) + 1
        if call_count > MAX_ORCHESTRATOR_CALLS:
            decision = _limit_decision()
        elif self.orchestrator:
            try:
                decision, header_streamed, response_streamed = (
                    await self._stream_orchestrator_decision(state, call_count)
                )
                state["decision_header_streamed"] = header_streamed
                state["response_streamed"] = response_streamed
            except (ValidationError, ValueError, TypeError) as exc:
                decision = OrchestratorDecision(
                    type="clarification",
                    response="我还不能准确判断你的需求。请补充具体商品、订单或想咨询的问题。",
                    reason=f"invalid_orchestrator_response:{type(exc).__name__}",
                )
                state["decision_header_streamed"] = False
                state["response_streamed"] = False
        else:
            decision = self._fallback_orchestrator_decision(state)
            state["decision_header_streamed"] = False
            state["response_streamed"] = False

        decision = self._validate_decision_budget(state, decision, call_count)
        state["orchestrator_call_count"] = call_count
        state["decision"] = decision.model_dump(mode="json")
        state["intent"] = _tag_from_decision(decision, state.get("intent"))
        state["boundary"] = _boundary_from_decision(decision)
        return state

    async def _stream_orchestrator_decision(
        self,
        state: AgentState,
        call_count: int,
    ) -> tuple[OrchestratorDecision, bool, bool]:
        writer = get_stream_writer()
        parser = TerminalResponseStreamParser()
        aggregate: AIMessageChunk | None = None
        saw_tool_call = False
        header_streamed = False
        response_streamed = False

        async for chunk in self.orchestrator.astream(
            _orchestrator_messages(state, call_count)
        ):
            aggregate = chunk if aggregate is None else aggregate + chunk
            chunk_has_tool_call = bool(chunk.tool_call_chunks or chunk.tool_calls)
            if chunk_has_tool_call:
                if parser.has_streamable_response or response_streamed:
                    raise RuntimeError(
                        "orchestrator emitted a tool call after starting a user response"
                    )
                saw_tool_call = True

            text = _content_to_text(chunk.content)
            if not text or saw_tool_call:
                continue

            previous_type = parser.decision_type
            deltas = parser.feed(text)
            if previous_type is None and parser.decision_type is not None:
                writer(
                    {
                        "kind": "decision_header",
                        "decision_type": parser.decision_type,
                    }
                )
                header_streamed = True
            for delta in deltas:
                writer({"kind": "response_delta", "delta": delta})
                response_streamed = True

        if aggregate is None:
            raise ValueError("orchestrator returned no message chunks")
        if aggregate.tool_calls:
            return (
                decision_from_ai_message(
                    aggregate,
                    has_tool_results=bool(state.get("tool_results")),
                ),
                False,
                False,
            )
        if saw_tool_call:
            raise ValueError("orchestrator returned incomplete tool call chunks")

        decision = parser.finish()
        if not header_streamed:
            writer({"kind": "decision_header", "decision_type": decision.type})
            header_streamed = True
        return decision, header_streamed, response_streamed

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
            return OrchestratorDecision(
                type="clarification",
                response="我暂时无法安全选择业务工具，请换一种方式描述你的需求。",
                reason="unknown_or_empty_tool_call",
            )
        if state.get("tool_wave_count", 0) >= MAX_TOOL_WAVES or (
            call_count >= MAX_ORCHESTRATOR_CALLS
        ):
            return _limit_decision()
        return decision

    def _dispatch_decision(self, state: AgentState) -> str:
        decision_type = state["decision"]["type"]
        if decision_type == "tool_calls":
            return "execute"
        if decision_type == "handoff":
            return "handoff"
        if decision_type == "out_of_scope":
            return "out_of_scope"
        return "respond"

    async def _execute_tool_wave(self, state: AgentState) -> AgentState:
        decision = OrchestratorDecision.model_validate(state["decision"])
        repo = ConversationRepository(self.session)
        writer = get_stream_writer()
        calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []

        for planned_call in decision.tool_calls:
            call, applied_memory_ids = self._prepare_tool_call(state, planned_call)
            if applied_memory_ids:
                state["applied_memory_ids"] = list(
                    dict.fromkeys(
                        [*state.get("applied_memory_ids", []), *applied_memory_ids]
                    )
                )
            writer(
                {
                    "kind": "tool_call",
                    "tool_name": call.name,
                    "status": "started",
                    "input": call.arguments,
                }
            )
            contract = self.contract_provider.get_contract(call.name)
            if contract is None:
                execution = ToolExecutionResult(
                    tool_name=call.name,
                    ok=False,
                    error=ToolError(code="unknown_tool", message=f"unknown tool: {call.name}"),
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
                        error=ToolError(code="invalid_input", message=str(exc)),
                    )
                except Exception as exc:  # defensive orchestration boundary
                    execution = ToolExecutionResult(
                        tool_name=call.name,
                        ok=False,
                        error=ToolError(code=type(exc).__name__, message=str(exc)),
                    )

            call_json = call.model_dump(mode="json")
            execution_json = execution.model_dump(mode="json")
            calls.append(call_json)
            results.append(
                {
                    "tool_call_id": call.id,
                    "name": call.name,
                    "execution": execution_json,
                }
            )
            writer(
                {
                    "kind": "tool_call",
                    "tool_name": call.name,
                    "status": "completed" if execution.ok else "error",
                    "input": call.arguments,
                    "output": execution_json,
                }
            )
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

    def _prepare_tool_call(
        self,
        state: AgentState,
        call: PlannedToolCall,
    ) -> tuple[PlannedToolCall, list[int]]:
        if call.name == "catalog_search":
            request = CatalogSearchInput.model_validate(call.arguments)
            catalog_input, applied_memory_ids = _catalog_search_input(state, request)
            return (
                call.model_copy(
                    update={"arguments": catalog_input.model_dump(mode="json")}
                ),
                applied_memory_ids,
            )

        if call.name == "catalog_compare":
            request = CatalogCompareInput.model_validate(call.arguments)
            resolved_sku_ids = _resolve_compare_sku_ids(
                state["message"], state.get("working_memory", {})
            )
            if resolved_sku_ids:
                request = request.model_copy(update={"sku_ids": resolved_sku_ids})
            return call.model_copy(update={"arguments": request.model_dump(mode="json")}), []

        if call.name == "order_lookup":
            arguments = dict(call.arguments)
            arguments["order_id"] = _resolve_order_id(
                state["message"],
                arguments.get("order_id"),
                state.get("working_memory", {}),
                self.memory_service,
            )
            return call.model_copy(update={"arguments": arguments}), []

        if call.name in {"policy_search", "knowledge_search"} and _is_v2_policy_followup(
            state["message"], state.get("working_memory", {})
        ):
            arguments = dict(call.arguments)
            arguments["query"] = self.memory_service.resolve_knowledge_query(
                state["message"],
                _knowledge_memory_view(state.get("working_memory", {})),
            )
            return call.model_copy(update={"arguments": arguments}), []

        return call, []

    async def _finalize_response(self, state: AgentState) -> AgentState:
        decision = OrchestratorDecision.model_validate(state["decision"])
        state["answer"] = decision.response.strip() or _fallback_answer(state)
        state["suggested_actions"] = _suggest_actions(state)
        return state

    async def _render_handoff_template(self, state: AgentState) -> AgentState:
        boundary = boundary_for_classification("human_handoff_required")
        state["boundary"] = boundary.model_dump(mode="json")
        state["answer"] = boundary.display_message
        state["suggested_actions"] = _suggest_actions(state)
        return state

    async def _render_out_of_scope_template(self, state: AgentState) -> AgentState:
        boundary = boundary_for_classification("out_of_scope")
        state["boundary"] = boundary.model_dump(mode="json")
        state["answer"] = boundary.display_message
        state["suggested_actions"] = _suggest_actions(state)
        return state

    async def _persist_turn(self, state: AgentState) -> AgentState:
        state["assistant_metadata"] = {
            "intent": state["intent"],
            "decision": state["decision"],
            "boundary": state["boundary"],
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
            return OrchestratorDecision(
                type="grounded_response",
                response=_fallback_answer(state),
                reason="llm_not_configured",
            )

        boundary = classify_boundary(state["message"])
        if boundary.classification == "human_handoff_required":
            return OrchestratorDecision(type="handoff", reason=boundary.reason)
        if boundary.classification == "out_of_scope":
            return OrchestratorDecision(type="out_of_scope", reason=boundary.reason)
        if _is_identity_or_capability_question(state["message"]):
            return OrchestratorDecision(
                type="direct_response",
                response=(
                    "我是 PC 外设商城客服 AI，可以帮你推荐和对比外设、查询订单物流，"
                    "以及说明售后政策和选购知识。"
                ),
                reason="identity_or_capability_question",
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
            search = build_product_search(state["message"])
            catalog_input, applied_memory_ids = _catalog_search_input(state, search)
            state["applied_memory_ids"] = applied_memory_ids
            return _tool_decision(
                "catalog_search", catalog_input.model_dump(mode="json")
            )
        if intent == "order_status":
            return _tool_decision(
                "order_lookup",
                {
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
            )
        return _tool_decision(
            "knowledge_search",
            {"query": state["message"], "limit": 3, "retrieval_mode": "hybrid"},
        )

    async def _route_intent(self, state: AgentState) -> AgentState:
        """Compatibility seam for deterministic context-routing tests."""
        intent = _contextual_intent(
            state["message"],
            state.get("working_memory", {}),
            self.memory_service,
        )
        parsed: dict[str, Any] = {}
        if intent == "product_recommendation":
            parsed["product_search"] = build_product_search(
                state["message"]
            ).model_dump(mode="json")
        elif intent == "order_status":
            parsed["order_id"] = _resolve_order_id(
                state["message"],
                extract_order_id(state["message"]),
                state.get("working_memory", {}),
                self.memory_service,
            )
        state["intent"] = intent
        state["parsed"] = parsed
        return state

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


def _product_search_from_state(state: AgentState) -> ProductSearchRequest:
    product_search = state.get("parsed", {}).get("product_search")
    if product_search:
        return ProductSearchRequest.model_validate(product_search)
    return build_product_search(state["message"])


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


def _catalog_search_input(
    state: AgentState, search: ProductSearchRequest | CatalogSearchInput
) -> tuple[CatalogSearchInput, list[int]]:
    working_plan = _working_catalog_query_plan(state.get("working_memory", {}))
    requested_brands = (
        _dedupe_text([*search.brands, *([search.brand] if search.brand else [])])
        if isinstance(search, CatalogSearchInput)
        else []
    )
    explicit_excluded_brands = _dedupe_text(
        [
            *_excluded_brands_from_message(
                state["message"], working_plan, state.get("memory", [])
            ),
            *(
                search.excluded_brands
                if isinstance(search, CatalogSearchInput)
                else []
            ),
        ]
    )
    inferred_brands = [
        brand
        for brand in _brands_from_message(
            state["message"], working_plan, state.get("memory", [])
        )
        if brand.lower() not in {item.lower() for item in explicit_excluded_brands}
    ]
    explicit_brands = [
        brand
        for brand in (requested_brands or inferred_brands)
        if brand.lower() not in {item.lower() for item in explicit_excluded_brands}
    ]
    explicit_excluded_usage = _dedupe_text(
        [
            *_excluded_usage_from_message(state["message"]),
            *(search.excluded_usage if isinstance(search, CatalogSearchInput) else []),
        ]
    )
    explicit_usage = (
        None
        if explicit_excluded_usage
        else (
            search.usage
            if isinstance(search, CatalogSearchInput) and search.usage
            else _usage_from_message(state["message"])
        )
    )
    explicit_filters = dict(search.filters)
    if _has_negative_term(state["message"], "无线"):
        explicit_filters["connection_type"] = "Wired"
    elif _has_negative_term(state["message"], "有线"):
        explicit_filters["connection_type"] = "Wireless"

    is_v2_followup = bool(working_plan) and _is_v2_product_followup(
        state["message"], state.get("working_memory", {})
    )
    category_changed = False
    if is_v2_followup:
        previous_category = _optional_text(working_plan.get("category"))
        category_changed = bool(search.category) and _catalog_category_key(
            search.category
        ) != _catalog_category_key(previous_category)
        base_filters = (
            dict(working_plan["filters"])
            if not category_changed and isinstance(working_plan.get("filters"), dict)
            else {}
        )
        historical_brands = [] if category_changed else _text_list(working_plan.get("brands"))
        current_brands = explicit_brands or [
            brand
            for brand in historical_brands
            if brand.lower()
            not in {item.lower() for item in explicit_excluded_brands}
        ]
        excluded_brands = _dedupe_text(
            [
                *_text_list(working_plan.get("excluded_brands")),
                *explicit_excluded_brands,
            ]
        )
        included_brands = {brand.lower() for brand in current_brands}
        excluded_brands = [
            brand for brand in excluded_brands if brand.lower() not in included_brands
        ]
        excluded_usage = _dedupe_text(
            [
                *_text_list(working_plan.get("excluded_usage")),
                *explicit_excluded_usage,
            ]
        )
        current_usage = explicit_usage or (
            None
            if category_changed
            else _optional_text(working_plan.get("usage_scenario"))
        )
        if explicit_excluded_usage and current_usage in explicit_excluded_usage:
            current_usage = None
        if explicit_usage:
            excluded_usage = [item for item in excluded_usage if item != explicit_usage]
        keywords = [] if category_changed else _text_list(working_plan.get("keywords"))
        if isinstance(search, CatalogSearchInput) and search.keywords:
            keywords = _dedupe_text(search.keywords)
        previous_usage = _optional_text(working_plan.get("usage_scenario"))
        if explicit_usage and explicit_usage != previous_usage:
            keywords = [item for item in keywords if item != previous_usage]
            keywords = _dedupe_text([*keywords, explicit_usage])
        query = (
            search.query or state["message"]
            if category_changed
            else _optional_text(working_plan.get("query"))
            or search.query
            or state["message"]
        )
        category = search.category or previous_category
        min_price = (
            search.min_price
            if search.min_price is not None
            else working_plan.get("min_price")
        )
        max_price = (
            search.max_price
            if search.max_price is not None
            else working_plan.get("max_price")
        )
        current_filters = {**base_filters, **explicit_filters}
        sort = (
            search.sort
            if isinstance(search, CatalogSearchInput)
            and "sort" in search.model_fields_set
            else _catalog_sort(working_plan.get("sort"))
        )
        limit = _catalog_limit(working_plan.get("limit"), search.limit)
    else:
        current_brands = explicit_brands
        excluded_brands = explicit_excluded_brands
        excluded_usage = explicit_excluded_usage
        current_usage = explicit_usage
        query = search.query or state["message"]
        category = search.category or _optional_text(working_plan.get("category"))
        min_price = search.min_price
        max_price = search.max_price
        current_filters = explicit_filters
        keywords = search.keywords if isinstance(search, CatalogSearchInput) else []
        sort = search.sort if isinstance(search, CatalogSearchInput) else "recommend"
        limit = search.limit

    preference_defaults, applied_memory_ids = _catalog_preference_defaults(
        state,
        current_brands=current_brands,
        current_max_price=max_price,
        current_connection=current_filters.get("connection_type"),
        current_usage=current_usage,
        current_excluded_brands=excluded_brands,
        current_excluded_usage=excluded_usage,
        reset_working_category_defaults=category_changed,
    )
    return (
        CatalogSearchInput(
            query=query,
            category=category,
            brands=current_brands,
            min_price=min_price,
            max_price=max_price,
            filters=current_filters,
            keywords=keywords,
            usage=current_usage,
            sort=sort,
            excluded_brands=excluded_brands,
            excluded_usage=excluded_usage,
            preference_defaults=preference_defaults,
            limit=limit,
        ),
        applied_memory_ids,
    )


def _dedupe_text(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _catalog_sort(value: Any) -> str:
    return (
        str(value)
        if value in {"recommend", "sales", "price_asc", "price_desc", "stock"}
        else "recommend"
    )


def _catalog_limit(value: Any, fallback: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return min(20, max(1, value))
    return fallback


def _catalog_category_key(value: str | None) -> str | None:
    if not value:
        return None
    lowered = value.lower()
    aliases = {
        "mouse": "鼠标",
        "mice": "鼠标",
        "keyboard": "键盘",
        "keyboards": "键盘",
        "headphone": "耳机",
        "headphones": "耳机",
        "headset": "耳机",
        "monitor": "显示器",
        "monitors": "显示器",
        "webcam": "摄像头",
        "webcams": "摄像头",
    }
    return aliases.get(lowered, lowered)


def _catalog_preference_defaults(
    state: AgentState,
    *,
    current_brands: list[str],
    current_max_price: Decimal | None,
    current_connection: str | None,
    current_usage: str | None,
    current_excluded_brands: list[str],
    current_excluded_usage: list[str],
    reset_working_category_defaults: bool = False,
) -> tuple[dict[str, Any], list[int]]:
    plan = _working_catalog_query_plan(state.get("working_memory", {}))
    filters = plan.get("filters") if isinstance(plan.get("filters"), dict) else {}
    defaults: dict[str, Any] = {
        "brands": (
            []
            if reset_working_category_defaults
            else _text_list(plan.get("brands"))
        ),
        "excluded_brands": _text_list(plan.get("excluded_brands")),
        "excluded_usage": _text_list(plan.get("excluded_usage")),
        "max_price": plan.get("max_price"),
        "connection_type": (
            None
            if reset_working_category_defaults
            else _normalized_connection(
                filters.get("connection_type", filters.get("wireless"))
            )
        ),
        "usage": (
            None
            if reset_working_category_defaults
            else _optional_text(plan.get("usage_scenario"))
        ),
    }
    applied: list[int] = []
    current_values = {
        "brands": bool(current_brands),
        "max_price": current_max_price is not None,
        "connection_type": current_connection is not None,
        "usage": current_usage is not None,
        "excluded_brands": bool(current_brands or current_excluded_brands),
        "excluded_usage": bool(current_usage or current_excluded_usage),
    }
    if current_brands:
        defaults["excluded_brands"] = []
    if current_excluded_brands:
        defaults["brands"] = []
    if current_usage:
        defaults["excluded_usage"] = []
    if current_excluded_usage:
        defaults["usage"] = None
    for raw_memory in state.get("memory", []):
        if not isinstance(raw_memory, dict):
            continue
        value_json = raw_memory.get("value_json")
        if not isinstance(value_json, dict):
            continue
        default_key, default_value = _preference_default(raw_memory.get("key"), value_json)
        conflicts = {
            "brands": "excluded_brands",
            "excluded_brands": "brands",
            "usage": "excluded_usage",
            "excluded_usage": "usage",
        }
        if (
            default_key is None
            or default_value is None
            or current_values.get(default_key)
            or defaults.get(default_key)
            or defaults.get(conflicts.get(default_key, ""))
        ):
            continue
        defaults[default_key] = default_value
        memory_id = raw_memory.get("id")
        if not current_values[default_key] and isinstance(memory_id, int):
            applied.append(memory_id)
    return defaults, applied


def _preference_default(
    key: Any, value_json: dict[str, Any]
) -> tuple[str | None, Any]:
    if key == "brand_preference":
        if value_json.get("negated") is True:
            brand = _optional_text(value_json.get("brand"))
            return "excluded_brands", [brand] if brand else None
        brand = _optional_text(value_json.get("brand"))
        return "brands", [brand] if brand else None
    if key == "budget_preference" and value_json.get("maximum") is not False:
        return "max_price", value_json.get("amount")
    if key == "connection_preference":
        connection = _normalized_connection(value_json.get("preference"))
        if value_json.get("negated") is True:
            connection = {"Wireless": "Wired", "Wired": "Wireless"}.get(connection)
        return "connection_type", connection
    if key == "usage_preference":
        if value_json.get("negated") is True:
            usage = _optional_text(value_json.get("usage"))
            return "excluded_usage", [usage] if usage else None
        return "usage", _optional_text(value_json.get("usage"))
    return None, None


def _working_catalog_query_plan(working_memory: dict[str, Any]) -> dict[str, Any]:
    catalog = working_memory.get("catalog")
    if not isinstance(catalog, dict):
        return {}
    query_plan = catalog.get("query_plan")
    return query_plan if isinstance(query_plan, dict) else {}


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


def _brands_from_message(
    message: str, working_plan: dict[str, Any], memories: list[dict[str, Any]]
) -> list[str]:
    candidates = ["Logitech", "Razer", "SteelSeries", "罗技", "雷蛇", "赛睿"]
    candidates.extend(_text_list(working_plan.get("brands")))
    for memory in memories:
        value_json = memory.get("value_json") if isinstance(memory, dict) else None
        if isinstance(value_json, dict) and (brand := _optional_text(value_json.get("brand"))):
            candidates.append(brand)
    lowered = message.lower()
    matched = list(
        dict.fromkeys(brand for brand in candidates if brand.lower() in lowered)
    )
    generic_latin_brands = re.findall(r"\b[A-Z][A-Za-z-]{2,30}\b", message)
    ignored = {"FPS", "RGB", "PC", "Wireless", "Wired", "Gaming", "Office"}
    matched.extend(brand for brand in generic_latin_brands if brand not in ignored)
    chinese_brand_match = re.search(
        r"(?:要|选|买|换成|推荐)\s*([\u4e00-\u9fff]{2,6})(?:牌|品牌)", message
    )
    if chinese_brand_match:
        matched.append(chinese_brand_match.group(1))
    return list(dict.fromkeys(matched))


def _usage_from_message(message: str) -> str | None:
    lowered = message.lower()
    if "fps" in lowered or "游戏" in message or "gaming" in lowered:
        return "gaming"
    if "办公" in message or "office" in lowered:
        return "office"
    return None


def _excluded_brands_from_message(
    message: str, working_plan: dict[str, Any], memories: list[dict[str, Any]]
) -> list[str]:
    return [
        brand
        for brand in _brands_from_message(message, working_plan, memories)
        if _has_negative_term(message, brand)
    ]


def _excluded_usage_from_message(message: str) -> list[str]:
    excluded: list[str] = []
    if _has_negative_term(message, "游戏") or _has_negative_term(message, "gaming"):
        excluded.append("gaming")
    if _has_negative_term(message, "办公") or _has_negative_term(message, "office"):
        excluded.append("office")
    return excluded


def _has_negative_term(message: str, term: str) -> bool:
    return bool(
        re.search(
            rf"(?:不要|不喜欢|不偏好|排除|避开|别(?:用|要|选)?)[^，。；]{{0,32}}{re.escape(term)}",
            message,
            flags=re.IGNORECASE,
        )
    )


def _normalized_connection(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "Wireless" if value else "Wired"
    lowered = str(value).lower()
    if lowered in {"wireless", "无线"}:
        return "Wireless"
    if lowered in {"wired", "有线"}:
        return "Wired"
    return None


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None and str(value).strip() else None


def _legacy_memory_view(working_memory: dict[str, Any]) -> dict[str, Any]:
    order = working_memory.get("order")
    if not isinstance(order, dict) or order.get("last_order_id") is None:
        return working_memory
    return {**working_memory, "last_order_id": order["last_order_id"]}


def _tool_result_payload(result: Any) -> dict[str, Any]:
    if result.ok and result.output is not None:
        return result.output
    return result.model_dump(mode="json", exclude={"output"})


def _orchestrator_messages(
    state: AgentState,
    call_count: int,
) -> list[SystemMessage | HumanMessage | AIMessage | ToolMessage]:
    messages: list[SystemMessage | HumanMessage | AIMessage | ToolMessage] = [
        SystemMessage(content=ORCHESTRATOR_SYSTEM_PROMPT)
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
            content=build_orchestrator_input(
                message=state["message"],
                tool_wave_count=state.get("tool_wave_count", 0),
                orchestrator_call_count=call_count,
                memory_context={
                    "priority": (
                        "current request and tool results > working memory > "
                        "explicit user preferences > recent history"
                    ),
                    "working_memory": state.get("working_memory", {}),
                    "explicit_user_preferences": state.get("memory", []),
                },
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
                        "args": call["arguments"],
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
        tool_calls=[PlannedToolCall(id=f"fallback_{name}", name=name, arguments=arguments)],
    )


def _limit_decision() -> OrchestratorDecision:
    return OrchestratorDecision(
        type="clarification",
        response=(
            "这次请求需要的查询步骤超过了当前处理上限。请缩小问题范围，"
            "例如只查询一个订单、一个商品类别或一个政策问题。"
        ),
        reason="orchestration_limit_reached",
    )


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


def _fallback_answer(state: AgentState) -> str:
    products = state.get("products", [])
    if products:
        lines = ["我根据商品目录找到了这些候选："]
        for product in products[:3]:
            specs = "，".join(
                f"{key}: {value}" for key, value in list(product.specs.items())[:4]
            )
            suffix = f"，{specs}" if specs else ""
            lines.append(
                f"- {product.title}：¥{product.price}，库存 {product.stock}{suffix}。"
            )
        lines.append("告诉我主要用途后，我可以继续判断哪一款更适合；实际价格和库存以下单页为准。")
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
        lines = ["我根据知识库查到以下信息："]
        lines.extend(f"- {item.title}：{item.snippet}" for item in evidence)
        lines.append("依据：" + "、".join(item.title for item in evidence))
        return "\n".join(lines)

    facet_output = _latest_successful_tool_output(state, "catalog_facets")
    if facet_output is not None:
        return _catalog_facets_fallback_answer(facet_output)

    failed_results = [
        result
        for result in state.get("tool_results", [])
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
        return {"query": message, "facet": "brand", "limit": 20}
    if any(term in compact for term in ("品类", "类目", "商品类型", "外设类型")):
        return {"query": message, "facet": "category", "limit": 20}
    if any(
        term in compact
        for term in ("轴体", "刷新率", "分辨率", "连接方式", "dpi", "颜色")
    ):
        return {"query": message, "facet": "spec_value", "limit": 20}
    return None


def _latest_successful_tool_output(
    state: AgentState,
    tool_name: str,
) -> dict[str, Any] | None:
    for result in reversed(state.get("tool_results", [])):
        execution = result.get("execution", {})
        if result.get("name") == tool_name and execution.get("ok"):
            output = execution.get("output")
            if isinstance(output, dict):
                return output
    return None


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


def _is_identity_or_capability_question(message: str) -> bool:
    compact = message.lower().replace(" ", "")
    return any(
        term in compact
        for term in ["你是谁", "你是什么", "你能做什么", "你会什么", "怎么用你"]
    )


def _suggest_actions(state: AgentState) -> list[dict[str, Any]]:
    boundary = state["boundary"]["classification"]
    if boundary == "human_handoff_required":
        return [{"label": "转人工客服", "payload": _handoff_payload(state["message"])}]
    if boundary == "out_of_scope":
        return [{"label": "咨询外设推荐", "payload": {"message": "推荐 300 元以内无线鼠标"}}]
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


def _chunk_text(text: str, chunk_size: int = 12) -> list[str]:
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)] or [""]


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "".join(parts)
    return "" if content is None else str(content)
