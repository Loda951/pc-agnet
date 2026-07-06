import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.intent import (
    boundary_for_classification,
    build_product_search,
    classify_boundary,
    classify_intent,
    extract_order_id,
)
from app.agent.prompts import BOUNDARY_PROTOCOL_PROMPT, SYSTEM_PROMPT
from app.agent.state import AgentState
from app.core.config import Settings
from app.core.llm import build_chat_model
from app.models import AgentRun
from app.repositories.catalog import CatalogRepository
from app.repositories.conversations import ConversationRepository
from app.repositories.orders import OrderRepository
from app.schemas.catalog import ProductSearchRequest
from app.schemas.chat import (
    BoundaryClassification,
    ChatRequest,
    ChatResponse,
    EvidenceItem,
    SuggestedAction,
)
from app.services.knowledge_rag import ChromaKnowledgeService
from app.services.memory import MemoryService

SESSION_HISTORY_LIMIT = 12


class AgentRuntime:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        knowledge_service: ChromaKnowledgeService | None = None,
    ):
        self.session = session
        self.settings = settings
        self.llm = build_chat_model(settings)
        self.knowledge_service = knowledge_service or ChromaKnowledgeService(session, settings)
        self.memory_service = MemoryService()

    async def run(self, request: ChatRequest, user_id: int) -> ChatResponse:
        graph = self._build_graph()
        result: AgentState = await graph.ainvoke(
            {
                "user_id": user_id,
                "conversation_id": request.conversation_id,
                "message": request.message,
            }
        )
        return self._response_from_state(result)

    async def run_stream(
        self, request: ChatRequest, user_id: int
    ) -> AsyncIterator[dict[str, Any]]:
        state: AgentState = {
            "user_id": user_id,
            "conversation_id": request.conversation_id,
            "message": request.message,
        }
        try:
            state = await self._load_context(state)
            yield _stream_event("run_started", state)

            state = await self._classify_boundary(state)
            yield _stream_event("boundary", state, boundary=state["boundary"])

            if state["boundary"]["classification"] == "in_scope_auto":
                state = await self._route_intent(state)
                yield _stream_event(
                    "tool_call",
                    state,
                    tool_name="intent.route",
                    status="completed",
                    output={"intent": state["intent"], "parsed": state.get("parsed", {})},
                )
                async for event in self._retrieve_stream(state):
                    yield event
                async for event in self._retrieve_knowledge_stream(state):
                    yield event

            answer_parts: list[str] = []
            async for delta in self._answer_deltas(state):
                if not delta:
                    continue
                answer_parts.append(delta)
                yield _stream_event("delta", state, delta=delta)

            state["answer"] = "".join(answer_parts)
            state["suggested_actions"] = self._suggest_actions(state)
            state = await self._persist(state)
            yield _stream_event(
                "done",
                state,
                response=self._response_from_state(state).model_dump(mode="json"),
            )
        except asyncio.CancelledError:
            await self._mark_stream_failed(state, "cancelled", "client disconnected")
            raise
        except Exception as exc:
            await self._mark_stream_failed(state, type(exc).__name__, str(exc))
            yield _stream_event(
                "error",
                state,
                error_type=type(exc).__name__,
                message="AI 回复生成失败，请稍后重试。",
                retryable=True,
            )

    def _build_graph(self):
        workflow = StateGraph(AgentState)
        workflow.add_node("load_context", self._load_context)
        workflow.add_node("classify_boundary", self._classify_boundary)
        workflow.add_node("route_intent", self._route_intent)
        workflow.add_node("retrieve", self._retrieve)
        workflow.add_node("retrieve_knowledge", self._retrieve_knowledge)
        workflow.add_node("generate", self._generate)
        workflow.add_node("persist", self._persist)
        workflow.set_entry_point("load_context")
        workflow.add_edge("load_context", "classify_boundary")
        workflow.add_conditional_edges(
            "classify_boundary",
            self._route_by_boundary,
            {"auto": "route_intent", "blocked": "generate"},
        )
        workflow.add_edge("route_intent", "retrieve")
        workflow.add_edge("retrieve", "retrieve_knowledge")
        workflow.add_edge("retrieve_knowledge", "generate")
        workflow.add_edge("generate", "persist")
        workflow.add_edge("persist", END)
        return workflow.compile()

    async def _load_context(self, state: AgentState) -> AgentState:
        repo = ConversationRepository(self.session)
        conversation = await repo.get_or_create(state["user_id"], state.get("conversation_id"))
        history = await repo.list_recent_messages(conversation.id, SESSION_HISTORY_LIMIT)
        working_memory = await repo.get_working_memory(conversation.id)
        await repo.add_message(conversation.id, "user", state["message"])
        run = await repo.start_run(conversation.id)
        memory = await repo.list_memory(state["user_id"])
        state["conversation_id"] = conversation.id
        state["run_id"] = run.id
        state["history"] = [
            {"role": item.role, "content": item.content}
            for item in history
            if item.role in {"user", "assistant"}
        ]
        state["working_memory"] = self.memory_service.normalize_working_memory(working_memory)
        state["memory"] = [
            {"key": item.key, "value": item.value, "confidence": item.confidence} for item in memory
        ]
        return state

    async def _classify_boundary(self, state: AgentState) -> AgentState:
        boundary = classify_boundary(state["message"])
        state["boundary"] = boundary.model_dump(mode="json")
        if boundary.classification != "in_scope_auto":
            state["intent"] = (
                "out_of_scope"
                if boundary.classification == "out_of_scope"
                else classify_intent(state["message"])
            )
            state["parsed"] = {}
        return state

    def _route_by_boundary(self, state: AgentState) -> str:
        boundary = state["boundary"]["classification"]
        return "auto" if boundary == "in_scope_auto" else "blocked"

    async def _route_intent(self, state: AgentState) -> AgentState:
        intent = self.memory_service.resolve_intent(
            state["message"],
            classify_intent(state["message"]),
            state.get("working_memory", {}),
        )
        parsed: dict[str, Any] = {}
        if intent == "product_recommendation":
            search = self.memory_service.resolve_product_search(
                state["message"],
                build_product_search(state["message"]),
                state.get("working_memory", {}),
            )
            parsed["product_search"] = search.model_dump(mode="json")
            referenced_product = self.memory_service.resolve_referenced_product(
                state["message"],
                state.get("working_memory", {}),
            )
            if referenced_product:
                parsed["referenced_product"] = referenced_product
        elif intent == "order_status":
            parsed["order_id"] = self.memory_service.resolve_order_id(
                state["message"],
                extract_order_id(state["message"]),
                state.get("working_memory", {}),
            )
        state["intent"] = intent
        state["parsed"] = parsed
        state["evidence"] = []
        return state

    async def _retrieve(self, state: AgentState) -> AgentState:
        repo = ConversationRepository(self.session)
        if state["intent"] == "product_recommendation":
            search = _product_search_from_state(state)
            products = await CatalogRepository(self.session).search_products(search)
            state["products"] = products
            await repo.add_tool_call(
                state["run_id"],
                "catalog.search_products",
                search.model_dump(mode="json"),
                {"count": len(products), "products": [p.model_dump(mode="json") for p in products]},
            )
        elif state["intent"] == "order_status":
            order_id = state.get("parsed", {}).get("order_id")
            orders = OrderRepository(self.session)
            order = (
                await orders.get_order(state["user_id"], order_id)
                if order_id
                else await orders.latest_order(state["user_id"])
            )
            state["order"] = order
            await repo.add_tool_call(
                state["run_id"],
                "order.get_order",
                {"order_id": order_id},
                {
                    "found": order is not None,
                    "order": order.model_dump(mode="json") if order else None,
                },
            )
        return state

    async def _retrieve_stream(self, state: AgentState) -> AsyncIterator[dict[str, Any]]:
        repo = ConversationRepository(self.session)
        if state["intent"] == "product_recommendation":
            search = _product_search_from_state(state)
            input_json = search.model_dump(mode="json")
            yield _stream_event(
                "tool_call",
                state,
                tool_name="catalog.search_products",
                status="started",
                input=input_json,
            )
            products = await CatalogRepository(self.session).search_products(search)
            state["products"] = products
            output_json = {
                "count": len(products),
                "products": [p.model_dump(mode="json") for p in products],
            }
            await repo.add_tool_call(
                state["run_id"],
                "catalog.search_products",
                input_json,
                output_json,
            )
            yield _stream_event(
                "tool_call",
                state,
                tool_name="catalog.search_products",
                status="completed",
                output=output_json,
            )
            yield _context_event(state)
        elif state["intent"] == "order_status":
            order_id = state.get("parsed", {}).get("order_id")
            input_json = {"order_id": order_id}
            yield _stream_event(
                "tool_call",
                state,
                tool_name="order.get_order",
                status="started",
                input=input_json,
            )
            orders = OrderRepository(self.session)
            order = (
                await orders.get_order(state["user_id"], order_id)
                if order_id
                else await orders.latest_order(state["user_id"])
            )
            state["order"] = order
            output_json = {
                "found": order is not None,
                "order": order.model_dump(mode="json") if order else None,
            }
            await repo.add_tool_call(
                state["run_id"],
                "order.get_order",
                input_json,
                output_json,
            )
            yield _stream_event(
                "tool_call",
                state,
                tool_name="order.get_order",
                status="completed",
                output=output_json,
            )
            yield _context_event(state)

    async def _retrieve_knowledge(self, state: AgentState) -> AgentState:
        repo = ConversationRepository(self.session)
        query = self.memory_service.resolve_knowledge_query(
            state["message"], state.get("working_memory", {})
        )
        try:
            evidence = await self.knowledge_service.retrieve(query)
            state["evidence"] = evidence
            await repo.add_tool_call(
                state["run_id"],
                "knowledge.retrieve",
                {"query": query, "limit": 3},
                {
                    "count": len(evidence),
                    "evidence": [item.model_dump(mode="json") for item in evidence],
                },
            )
        except Exception as exc:
            state["evidence"] = []
            await repo.add_tool_call(
                state["run_id"],
                "knowledge.retrieve",
                {"query": state["message"], "limit": 3},
                {"error": type(exc).__name__, "message": str(exc)},
            )
        return state

    async def _retrieve_knowledge_stream(
        self, state: AgentState
    ) -> AsyncIterator[dict[str, Any]]:
        repo = ConversationRepository(self.session)
        query = self.memory_service.resolve_knowledge_query(
            state["message"], state.get("working_memory", {})
        )
        input_json = {"query": query, "limit": 3}
        yield _stream_event(
            "tool_call",
            state,
            tool_name="knowledge.retrieve",
            status="started",
            input=input_json,
        )
        try:
            evidence = await self.knowledge_service.retrieve(query)
            state["evidence"] = evidence
            output_json = {
                "count": len(evidence),
                "evidence": [item.model_dump(mode="json") for item in evidence],
            }
            await repo.add_tool_call(
                state["run_id"],
                "knowledge.retrieve",
                input_json,
                output_json,
            )
            yield _stream_event(
                "tool_call",
                state,
                tool_name="knowledge.retrieve",
                status="completed",
                output=output_json,
            )
            yield _context_event(state)
        except Exception as exc:
            state["evidence"] = []
            output_json = {"error": type(exc).__name__, "message": str(exc)}
            await repo.add_tool_call(
                state["run_id"],
                "knowledge.retrieve",
                input_json,
                output_json,
            )
            yield _stream_event(
                "tool_call",
                state,
                tool_name="knowledge.retrieve",
                status="error",
                output=output_json,
            )

    async def _generate(self, state: AgentState) -> AgentState:
        if state["boundary"]["classification"] != "in_scope_auto":
            state["answer"] = self._generate_boundary_answer(state)
        elif self.llm:
            state["answer"] = await self._generate_with_llm(state)
        else:
            state["answer"] = self._generate_fallback(state)
        state["suggested_actions"] = self._suggest_actions(state)
        return state

    async def _persist(self, state: AgentState) -> AgentState:
        state["intent"] = _final_intent(state)
        repo = ConversationRepository(self.session)
        await repo.add_message(
            state["conversation_id"],
            "assistant",
            state["answer"],
            {
                "intent": state["intent"],
                "boundary": state["boundary"],
                "evidence": _dump_evidence(state.get("evidence", [])),
                "products": [p.model_dump(mode="json") for p in state.get("products", [])],
                "order": state["order"].model_dump(mode="json") if state.get("order") else None,
            },
        )
        if state["boundary"]["classification"] == "in_scope_auto":
            await self._maybe_update_memory(repo, state)
        next_working_memory = self.memory_service.update_after_turn(
            state.get("working_memory", {}),
            state,
        )
        state["working_memory"] = next_working_memory
        await repo.update_working_memory(state["conversation_id"], next_working_memory)
        run = await self.session.get(AgentRun, state["run_id"])
        if run:
            await repo.finish_run(run, state["intent"], _json_safe_state(state))
        await self.session.commit()
        return state

    async def _generate_with_llm(self, state: AgentState) -> str:
        response = await self.llm.ainvoke(_llm_messages(state))
        raw_text = _content_to_text(response.content)
        return self._resolve_protocol_completion(state, raw_text)

    async def _answer_deltas(self, state: AgentState) -> AsyncIterator[str]:
        if state["boundary"]["classification"] != "in_scope_auto":
            for chunk in _chunk_text(self._generate_boundary_answer(state)):
                await asyncio.sleep(0)
                yield chunk
            return

        if self.llm:
            async for delta in self._stream_llm_with_protocol(state):
                yield delta
            return

        for chunk in _chunk_text(self._generate_fallback(state)):
            await asyncio.sleep(0)
            yield chunk

    def _resolve_protocol_completion(self, state: AgentState, raw_text: str) -> str:
        protocol = _parse_boundary_protocol(raw_text)
        if protocol is None:
            return raw_text

        if protocol == "in_scope_auto":
            body = _protocol_body(raw_text)
            return body if body.strip() else self._generate_fallback(state)

        self._apply_runtime_boundary(state, protocol)
        return self._generate_boundary_answer(state)

    async def _stream_llm_with_protocol(self, state: AgentState) -> AsyncIterator[str]:
        header_buffer = ""
        protocol: str | None = None
        emitted_body = False
        handed_off = False

        async for chunk in self.llm.astream(_llm_messages(state)):
            text = _content_to_text(chunk.content)
            if not text:
                continue

            if protocol is None:
                header_buffer += text
                if "\n" not in header_buffer:
                    continue

                first_line, remainder = _split_first_line(header_buffer)
                parsed = _parse_boundary_header(first_line)
                if parsed is None:
                    protocol = "in_scope_auto"
                    if header_buffer:
                        emitted_body = bool(header_buffer.strip()) or emitted_body
                        yield header_buffer
                    header_buffer = ""
                    continue

                protocol = parsed
                header_buffer = ""
                if protocol == "in_scope_auto":
                    if remainder:
                        emitted_body = bool(remainder.strip()) or emitted_body
                        yield remainder
                    continue

                self._apply_runtime_boundary(state, protocol)
                handed_off = True
                for boundary_chunk in _chunk_text(self._generate_boundary_answer(state)):
                    yield boundary_chunk
                continue

            if handed_off:
                continue

            emitted_body = bool(text.strip()) or emitted_body
            yield text

        if protocol is None and header_buffer:
            parsed = _parse_boundary_header(header_buffer)
            if parsed is None:
                yield header_buffer
                return

            if parsed == "in_scope_auto":
                for chunk in _chunk_text(self._generate_fallback(state)):
                    await asyncio.sleep(0)
                    yield chunk
                return

            self._apply_runtime_boundary(state, parsed)
            for chunk in _chunk_text(self._generate_boundary_answer(state)):
                yield chunk
            return

        if protocol == "in_scope_auto" and not emitted_body:
            for chunk in _chunk_text(self._generate_fallback(state)):
                await asyncio.sleep(0)
                yield chunk

    def _apply_runtime_boundary(self, state: AgentState, classification: str) -> None:
        reason = {
            "human_handoff_required": "生成阶段识别为需要人工确认或执行的请求",
            "out_of_scope": "生成阶段识别为超出 PC 外设商城客服范围",
        }.get(classification)
        state["boundary"] = boundary_for_classification(
            classification,
            reason=reason,
        ).model_dump(mode="json")
        state["intent"] = classification

    def _generate_boundary_answer(self, state: AgentState) -> str:
        boundary = BoundaryClassification(**state["boundary"])
        return boundary.display_message

    def _generate_fallback(self, state: AgentState) -> str:
        evidence = _normalize_evidence(state.get("evidence", []))
        if state["intent"] == "product_recommendation":
            referenced_product = state.get("parsed", {}).get("referenced_product")
            if referenced_product:
                specs = (
                    "，".join(
                        f"{key}: {value}"
                        for key, value in referenced_product.get("specs", {}).items()
                    )
                    or "规格未标注"
                )
                return _append_evidence(
                    f"你说的是 {referenced_product['title']}："
                    f"¥{referenced_product['price']}，库存 {referenced_product['stock']}，"
                    f"{specs}。如果要继续对比，我可以拿它和上一组里的其他候选逐项比较。",
                    evidence,
                )
            products = state.get("products", [])
            if not products:
                return (
                    "我暂时没有找到完全匹配的商品。你可以告诉我预算、用途、"
                    "偏好的连接方式或品牌，我再缩小范围。"
                )
            lines = ["我先按你的需求筛了这些可售 SKU："]
            for item in products[:3]:
                specs = (
                    "，".join(f"{key}: {value}" for key, value in item.specs.items())
                    or "规格未标注"
                )
                lines.append(
                    f"- {item.title}：¥{item.price}，库存 {item.stock}，{specs}。"
                    f"适配提示：{_compatibility_note(item)}"
                )
            lines.append(
                "如果你告诉我主要用途，比如 FPS、办公、剪辑或通勤，我可以再给你排个优先级。"
            )
            return _append_evidence("\n".join(lines), evidence)

        if state["intent"] == "order_status":
            order = state.get("order")
            if not order:
                return "我没查到这个订单。请确认订单号，或直接说“查最近订单”。"
            logistics = order.logistics
            ship_line = "暂未发货"
            if logistics and logistics.logistic_no:
                ship_line = f"{logistics.express_company or '快递'} {logistics.logistic_no}"
            return _append_evidence(
                f"订单 {order.id} 当前是「{order.status_label}」，实付 ¥{order.pay_amount}。\n"
                f"物流：{ship_line}。\n"
                f"订单里共有 {len(order.items)} 个明细，"
                "需要退换货的话，我可以帮你整理信息并转人工处理。",
                evidence,
            )

        if state["intent"] == "after_sales":
            if evidence:
                lines = ["我查到这些售后政策依据："]
                lines.extend(_evidence_lines(evidence))
                lines.append(
                    "如果要申请办理、确认责任或承诺退款，需要转人工客服处理。"
                )
                return "\n".join(lines)
            return (
                "我可以说明退换货、退款和维修的基础流程；如果要申请办理、确认责任或承诺退款，"
                "需要转人工客服处理。"
            )

        if state["intent"] == "purchase_guidance":
            return _append_evidence(
                "下单流程可以按这几步走：\n"
                "1. 先在商品推荐或商品详情里确认 SKU、价格、库存和关键规格。\n"
                "2. 到商城商品页选择对应规格，填写或确认收货信息。\n"
                "3. 提交订单后按页面提示完成支付，再到订单页查看状态和物流。\n"
                "我不能在聊天里替你提交订单或完成支付；如果遇到支付异常、订单修改或"
                "其他需要人工确认的问题，我可以帮你整理信息并转人工处理。",
                evidence,
            )

        if evidence:
            return "\n".join(["我按知识库信息先回答：", *_evidence_lines(evidence)])

        return (
            "我可以帮你推荐 PC 外设、查询订单物流，也可以说明售后流程。"
            "涉及退换货、退款或维修办理时会转人工。"
        )

    def _suggest_actions(self, state: AgentState) -> list[dict[str, Any]]:
        boundary = state["boundary"]["classification"]
        if boundary == "human_handoff_required":
            payload: dict[str, Any] = {"handoff": True}
            payload.update(
                self.memory_service.build_handoff_draft(
                    state["message"],
                    state.get("working_memory", {}),
                )
            )
            return [{"label": "转人工客服", "payload": payload}]
        if boundary == "out_of_scope":
            return [{"label": "咨询外设推荐", "payload": {"message": "推荐 300 元以内无线鼠标"}}]
        if state["intent"] == "product_recommendation" and state.get("products"):
            return [
                {"label": "查询最近订单", "payload": {"message": "帮我查最近订单"}},
                {"label": "换成无线", "payload": {"message": "推荐无线款"}},
            ]
        if state["intent"] == "order_status" and state.get("order"):
            return [{"label": "转人工处理售后", "payload": {"orderId": state["order"].id}}]
        if state["intent"] == "purchase_guidance":
            return [
                {"label": "继续选购外设", "payload": {"message": "推荐 300 元以内无线鼠标"}},
                {"label": "查询最近订单", "payload": {"message": "帮我查最近订单"}},
            ]
        return []

    async def _maybe_update_memory(self, repo: ConversationRepository, state: AgentState) -> None:
        message = state["message"]
        if "无线" in message:
            await repo.upsert_memory(state["user_id"], "connection_preference", "偏好无线设备", 0.8)
        if "fps" in message.lower() or "游戏" in message:
            await repo.upsert_memory(state["user_id"], "usage_preference", "偏好游戏场景", 0.75)

    async def _mark_stream_failed(
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
            intent=_final_intent(state),
            boundary=BoundaryClassification(**state["boundary"]),
            evidence=_normalize_evidence(state.get("evidence", [])),
            products=state.get("products", []),
            order=state.get("order"),
            suggested_actions=[
                SuggestedAction(**item) for item in state.get("suggested_actions", [])
            ],
        )


def _llm_messages(state: AgentState) -> list[SystemMessage | HumanMessage | AIMessage]:
    context = {
        "intent": state["intent"],
        "boundary": state["boundary"],
        "parsed": state.get("parsed", {}),
        "memory": state.get("memory", []),
        "working_memory": state.get("working_memory", {}),
        "evidence": _dump_evidence(state.get("evidence", [])),
        "products": [p.model_dump(mode="json") for p in state.get("products", [])],
        "order": state["order"].model_dump(mode="json") if state.get("order") else None,
    }
    messages: list[SystemMessage | HumanMessage | AIMessage] = [
        SystemMessage(content=f"{SYSTEM_PROMPT}\n\n{BOUNDARY_PROTOCOL_PROMPT}")
    ]
    for item in state.get("history", []):
        content = item.get("content", "")
        if not content:
            continue
        if item.get("role") == "user":
            messages.append(HumanMessage(content=content))
        elif item.get("role") == "assistant":
            messages.append(AIMessage(content=content))
    messages.append(HumanMessage(content=f"用户问题：{state['message']}\n检索上下文：{context}"))
    return messages


def _product_search_from_state(state: AgentState) -> ProductSearchRequest:
    product_search = state.get("parsed", {}).get("product_search")
    if product_search:
        return ProductSearchRequest.model_validate(product_search)
    return build_product_search(state["message"])


def _json_safe_state(state: AgentState) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in state.items():
        if key == "products":
            result[key] = [item.model_dump(mode="json") for item in value]
        elif key == "evidence":
            result[key] = _dump_evidence(value)
        elif key == "order" and value is not None:
            result[key] = value.model_dump(mode="json")
        elif isinstance(value, Decimal):
            result[key] = str(value)
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
        products=[p.model_dump(mode="json") for p in state.get("products", [])],
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
                text = item.get("text") or item.get("content") or ""
                parts.append(str(text))
            else:
                parts.append(str(item))
        return "".join(parts)
    return "" if content is None else str(content)


def _split_first_line(text: str) -> tuple[str, str]:
    normalized = text.replace("\r\n", "\n")
    if "\n" not in normalized:
        return normalized, ""
    first_line, remainder = normalized.split("\n", 1)
    return first_line, remainder


def _parse_boundary_header(line: str) -> str | None:
    normalized = line.strip().upper()
    if normalized == "BOUNDARY: IN_SCOPE":
        return "in_scope_auto"
    if normalized == "BOUNDARY: HANDOFF":
        return "human_handoff_required"
    if normalized == "BOUNDARY: OOS":
        return "out_of_scope"
    return None


def _parse_boundary_protocol(text: str) -> str | None:
    first_line, _ = _split_first_line(text)
    return _parse_boundary_header(first_line)


def _protocol_body(text: str) -> str:
    _, remainder = _split_first_line(text)
    return remainder


def _final_intent(state: AgentState) -> str:
    boundary = state.get("boundary", {}).get("classification")
    if boundary in {"human_handoff_required", "out_of_scope"} and state.get("intent") == "general":
        return str(boundary)
    return state.get("intent", "general")


def _normalize_evidence(items: list[EvidenceItem | dict[str, Any]]) -> list[EvidenceItem]:
    return [item if isinstance(item, EvidenceItem) else EvidenceItem(**item) for item in items]


def _dump_evidence(items: list[EvidenceItem | dict[str, Any]]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in _normalize_evidence(items)]


def _append_evidence(answer: str, evidence: list[EvidenceItem]) -> str:
    if not evidence:
        return answer
    return "\n\n参考依据：\n" + "\n".join(_evidence_lines(evidence))


def _evidence_lines(evidence: list[EvidenceItem]) -> list[str]:
    return [f"- [{item.document_type}] {item.title}：{item.snippet}" for item in evidence[:3]]


def _compatibility_note(item: Any) -> str:
    specs = {str(key): str(value) for key, value in item.specs.items()}
    category = item.category
    if category == "鼠标":
        connection = specs.get("connection_type", "连接方式未标注")
        dpi = specs.get("max_dpi", "DPI 未标注")
        hand = specs.get("hand_orientation", "握持方向未标注")
        return f"{connection}，{dpi} DPI，上手方向 {hand}，适合按连接方式和握持习惯筛选。"
    if category == "键盘":
        connection = specs.get("connection_type", "连接方式未标注")
        switches = specs.get("switches", "轴体未标注")
        layout = "无数字区" if specs.get("tenkeyless") == "是" else "全尺寸或未标注布局"
        return f"{connection}，{switches}，{layout}，适合按轴体、灯光和桌面空间对比。"
    if category == "耳机":
        wireless = "无线" if specs.get("wireless") == "是" else "有线或未标注"
        microphone = "带麦克风" if specs.get("microphone") == "是" else "不带麦或未标注"
        enclosure = specs.get("enclosure_type", "封闭/开放类型未标注")
        return f"{wireless}，{microphone}，{enclosure}，适合按平台连接和通话需求确认兼容性。"
    return "建议继续确认连接方式、接口、尺寸和使用场景。"
