import asyncio
import json
from collections.abc import AsyncIterator
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
from app.agent.tooling import (
    RegistryToolExecutor,
    StaticToolContractProvider,
    ToolContractProvider,
    ToolExecutor,
)
from app.core.config import Settings
from app.core.llm import build_chat_model
from app.models import AgentRun
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
from app.tools.schemas import ToolError, ToolExecutionResult

SESSION_HISTORY_LIMIT = 6
MAX_ORCHESTRATOR_CALLS = 3
MAX_TOOL_WAVES = 2


class AgentRuntime:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        *,
        contract_provider: ToolContractProvider | None = None,
        tool_executor: ToolExecutor | None = None,
        chat_model: Any | None = None,
    ):
        self.session = session
        self.settings = settings
        self.contract_provider = contract_provider or StaticToolContractProvider()
        self.tool_executor = tool_executor or RegistryToolExecutor(session, settings)
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
        repo = ConversationRepository(self.session)
        conversation = await repo.get_or_create(state["user_id"], state.get("conversation_id"))
        history = await repo.list_recent_messages(conversation.id, SESSION_HISTORY_LIMIT)
        user_message = await repo.add_message(conversation.id, "user", state["message"])
        run = await repo.start_run(conversation.id)
        state["conversation_id"] = conversation.id
        state["user_message_id"] = user_message.id
        state["run_id"] = run.id
        state["history"] = [
            {"role": item.role, "content": item.content}
            for item in history
            if item.role in {"user", "assistant"}
        ]
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

        for call in decision.tool_calls:
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
        repo = ConversationRepository(self.session)
        await repo.add_message(
            state["conversation_id"],
            "assistant",
            state["answer"],
            {
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
            },
        )
        run = await self.session.get(AgentRun, state["run_id"])
        if run:
            await repo.finish_run(run, state["intent"], _json_safe_state(state))
        await self.session.commit()
        return state

    async def _route_intent(self, state: AgentState) -> AgentState:
        """Compatibility shim for older working-memory tests."""
        message = state.get("message", "")
        working_memory = state.get("working_memory", {}) or {}
        parsed = dict(state.get("parsed", {}) or {})

        recent_products = working_memory.get("recent_products") or []
        if recent_products and _mentions_second_item(message) and len(recent_products) >= 2:
            state["intent"] = "product_recommendation"
            parsed["referenced_product"] = recent_products[1]
            state["parsed"] = parsed
            return state

        current_search = working_memory.get("current_product_search")
        if current_search:
            search = dict(current_search)
            filters = dict(search.get("filters", {}) or {})
            if _mentions_wireless(message):
                filters["connection_type"] = "Wireless"
            search["filters"] = filters
            parsed["product_search"] = search
            state["intent"] = "product_recommendation"
            state["parsed"] = parsed
            return state

        if working_memory.get("last_order_id") and _mentions_order(message):
            state["intent"] = "order_status"
            parsed["order_id"] = working_memory["last_order_id"]
            state["parsed"] = parsed
            return state

        if working_memory.get("last_policy_query"):
            state["intent"] = "after_sales"
            state["parsed"] = parsed
            return state

        state["intent"] = classify_intent(message)
        state["parsed"] = parsed
        return state

    def _suggest_actions(self, state: AgentState) -> list[dict[str, Any]]:
        return _suggest_actions(state)

    def _generate_fallback(self, state: AgentState) -> str:
        return _fallback_answer(state)

    def _fallback_orchestrator_decision(self, state: AgentState) -> OrchestratorDecision:
        if state.get("tool_results"):
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

        intent = classify_intent(state["message"])
        if intent == "product_recommendation":
            search = build_product_search(state["message"])
            return _tool_decision("catalog_search", search.model_dump(mode="json"))
        if intent == "order_status":
            return _tool_decision(
                "order_lookup",
                {"order_id": extract_order_id(state["message"]), "limit": 5},
            )
        if intent == "after_sales":
            return _tool_decision(
                "policy_search",
                {"query": state["message"], "limit": 3, "retrieval_mode": "hybrid"},
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

    async def _mark_run_failed(
        self, state: AgentState, error_type: str, message: str
    ) -> None:
        if not state.get("run_id"):
            return
        repo = ConversationRepository(self.session)
        await repo.fail_run(
            state["run_id"],
            state.get("intent"),
            _json_safe_state(state),
            {"type": error_type, "message": message},
        )
        await self.session.commit()

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
        )


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


def _llm_messages(
    state: AgentState,
) -> list[SystemMessage | HumanMessage | AIMessage | ToolMessage]:
    messages = _orchestrator_messages(state, call_count=1)
    working_memory = state.get("working_memory")
    if working_memory and isinstance(messages[-1], HumanMessage):
        messages[-1] = HumanMessage(
            content=(
                str(messages[-1].content)
                + "\nworking_memory: "
                + json.dumps(working_memory, ensure_ascii=False)
            )
        )
    return messages



def _mentions_wireless(message: str) -> bool:
    lowered = message.lower()
    return "wireless" in lowered or "\u65e0\u7ebf" in message


def _mentions_order(message: str) -> bool:
    lowered = message.lower()
    return "order" in lowered or "\u8ba2\u5355" in message or "\u7269\u6d41" in message


def _mentions_second_item(message: str) -> bool:
    lowered = message.lower()
    return "second" in lowered or "\u7b2c\u4e8c" in message or "\u7b2c2" in message

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
                self_name in _LLM_TOOL_NAMES for self_name in current_tag.split(" + ")
            )
            else []
        )
        return " + ".join(dict.fromkeys([*previous_tool_names, *tool_names]))
    return current_tag or "general"


_LLM_TOOL_NAMES = {
    "catalog_search",
    "catalog_compare",
    "order_lookup",
    "policy_search",
    "knowledge_search",
}


def _apply_tool_output(
    state: AgentState,
    call: PlannedToolCall,
    execution: ToolExecutionResult,
) -> None:
    if not execution.ok or not execution.output:
        return
    output = execution.output
    if call.name in {"catalog_search", "catalog_compare"}:
        state["products"] = [
            ProductCard.model_validate(product) for product in output.get("products", [])
        ]
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
    referenced_product = state.get("parsed", {}).get("referenced_product")
    if referenced_product:
        specs = referenced_product.get("specs", {}) or {}
        specs_text = "，".join(f"{key}: {value}" for key, value in list(specs.items())[:4])
        suffix = f"，{specs_text}" if specs_text else ""
        return (
            f"{referenced_product.get('title')}：Â¥{referenced_product.get('price')}，"
            f"库存 {referenced_product.get('stock')}{suffix}。"
        )

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

    failed_results = [
        result
        for result in state.get("tool_results", [])
        if not result.get("execution", {}).get("ok")
    ]
    if failed_results:
        return "业务信息查询暂时失败，请稍后重试或补充更具体的信息。"
    return "我暂时没有找到足够的信息，请补充具体商品、订单号或想咨询的政策。"


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
        payload = _handoff_payload(state["message"])
        if payload.get("orderId") is None:
            payload["orderId"] = (state.get("working_memory") or {}).get("last_order_id")
        return [{"label": "\u8f6c\u4eba\u5de5\u5ba2\u670d", "payload": payload}]
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
