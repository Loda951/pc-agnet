from decimal import Decimal
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.intent import build_product_search, classify_intent, extract_order_id
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.state import AgentState
from app.core.config import Settings
from app.core.llm import build_chat_model
from app.models import AgentRun
from app.repositories.catalog import CatalogRepository
from app.repositories.conversations import ConversationRepository
from app.repositories.orders import OrderRepository
from app.schemas.chat import ChatRequest, ChatResponse, SuggestedAction


class AgentRuntime:
    def __init__(self, session: AsyncSession, settings: Settings):
        self.session = session
        self.settings = settings
        self.llm = build_chat_model(settings)

    async def run(self, request: ChatRequest) -> ChatResponse:
        graph = self._build_graph()
        result: AgentState = await graph.ainvoke(
            {
                "user_id": request.user_id or self.settings.default_user_id,
                "conversation_id": request.conversation_id,
                "message": request.message,
            }
        )
        return ChatResponse(
            conversation_id=result["conversation_id"],
            answer=result["answer"],
            intent=result["intent"],
            products=result.get("products", []),
            order=result.get("order"),
            suggested_actions=[
                SuggestedAction(**item) for item in result.get("suggested_actions", [])
            ],
        )

    def _build_graph(self):
        workflow = StateGraph(AgentState)
        workflow.add_node("load_context", self._load_context)
        workflow.add_node("route_intent", self._route_intent)
        workflow.add_node("retrieve", self._retrieve)
        workflow.add_node("generate", self._generate)
        workflow.add_node("persist", self._persist)
        workflow.set_entry_point("load_context")
        workflow.add_edge("load_context", "route_intent")
        workflow.add_edge("route_intent", "retrieve")
        workflow.add_edge("retrieve", "generate")
        workflow.add_edge("generate", "persist")
        workflow.add_edge("persist", END)
        return workflow.compile()

    async def _load_context(self, state: AgentState) -> AgentState:
        repo = ConversationRepository(self.session)
        conversation = await repo.get_or_create(state["user_id"], state.get("conversation_id"))
        await repo.add_message(conversation.id, "user", state["message"])
        run = await repo.start_run(conversation.id)
        memory = await repo.list_memory(state["user_id"])
        state["conversation_id"] = conversation.id
        state["run_id"] = run.id
        state["memory"] = [
            {"key": item.key, "value": item.value, "confidence": item.confidence} for item in memory
        ]
        return state

    async def _route_intent(self, state: AgentState) -> AgentState:
        intent = classify_intent(state["message"])
        parsed: dict[str, Any] = {}
        if intent == "product_recommendation":
            parsed["product_search"] = build_product_search(state["message"]).model_dump(
                mode="json"
            )
        elif intent == "order_status":
            parsed["order_id"] = extract_order_id(state["message"])
        state["intent"] = intent
        state["parsed"] = parsed
        return state

    async def _retrieve(self, state: AgentState) -> AgentState:
        repo = ConversationRepository(self.session)
        if state["intent"] == "product_recommendation":
            search = build_product_search(state["message"])
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

    async def _generate(self, state: AgentState) -> AgentState:
        if self.llm:
            state["answer"] = await self._generate_with_llm(state)
        else:
            state["answer"] = self._generate_fallback(state)
        state["suggested_actions"] = self._suggest_actions(state)
        return state

    async def _persist(self, state: AgentState) -> AgentState:
        repo = ConversationRepository(self.session)
        await repo.add_message(
            state["conversation_id"],
            "assistant",
            state["answer"],
            {
                "intent": state["intent"],
                "products": [p.model_dump(mode="json") for p in state.get("products", [])],
                "order": state["order"].model_dump(mode="json") if state.get("order") else None,
            },
        )
        await self._maybe_update_memory(repo, state)
        run = await self.session.get(AgentRun, state["run_id"])
        if run:
            await repo.finish_run(run, state["intent"], _json_safe_state(state))
        await self.session.commit()
        return state

    async def _generate_with_llm(self, state: AgentState) -> str:
        context = {
            "intent": state["intent"],
            "memory": state.get("memory", []),
            "products": [p.model_dump(mode="json") for p in state.get("products", [])],
            "order": state["order"].model_dump(mode="json") if state.get("order") else None,
        }
        response = await self.llm.ainvoke(
            [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=f"用户问题：{state['message']}\n检索上下文：{context}"),
            ]
        )
        return str(response.content)

    def _generate_fallback(self, state: AgentState) -> str:
        if state["intent"] == "product_recommendation":
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
                lines.append(f"- {item.title}：¥{item.price}，库存 {item.stock}，{specs}")
            lines.append(
                "如果你告诉我主要用途，比如 FPS、办公、剪辑或通勤，我可以再给你排个优先级。"
            )
            return "\n".join(lines)

        if state["intent"] == "order_status":
            order = state.get("order")
            if not order:
                return "我没查到这个订单。请确认订单号，或直接说“查最近订单”。"
            logistics = order.logistics
            ship_line = "暂未发货"
            if logistics and logistics.logistic_no:
                ship_line = f"{logistics.express_company or '快递'} {logistics.logistic_no}"
            return (
                f"订单 {order.id} 当前是「{order.status_label}」，实付 ¥{order.pay_amount}。\n"
                f"物流：{ship_line}。\n"
                f"订单里共有 {len(order.items)} 个明细，"
                "需要退换货的话我可以基于具体明细创建售后工单。"
            )

        if state["intent"] == "after_sales":
            return (
                "可以创建退换货工单。请提供订单号、要处理的商品明细，以及是退货、换货、退款还是维修。"
                "如果你已经在订单卡片里看到商品，前端可以直接点“创建售后”。"
            )

        return (
            "我可以帮你推荐 PC 外设、查询订单物流，也可以创建退换货工单。你可以直接说预算和用途。"
        )

    def _suggest_actions(self, state: AgentState) -> list[dict[str, Any]]:
        if state["intent"] == "product_recommendation" and state.get("products"):
            return [
                {"label": "查询最近订单", "payload": {"message": "帮我查最近订单"}},
                {"label": "换成无线", "payload": {"message": "推荐无线款"}},
            ]
        if state["intent"] == "order_status" and state.get("order"):
            return [{"label": "创建售后工单", "payload": {"orderId": state["order"].id}}]
        return []

    async def _maybe_update_memory(self, repo: ConversationRepository, state: AgentState) -> None:
        message = state["message"]
        if "无线" in message:
            await repo.upsert_memory(state["user_id"], "connection_preference", "偏好无线设备", 0.8)
        if "fps" in message.lower() or "游戏" in message:
            await repo.upsert_memory(state["user_id"], "usage_preference", "偏好游戏场景", 0.75)


def _json_safe_state(state: AgentState) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in state.items():
        if key == "products":
            result[key] = [item.model_dump(mode="json") for item in value]
        elif key == "order" and value is not None:
            result[key] = value.model_dump(mode="json")
        elif isinstance(value, Decimal):
            result[key] = str(value)
        else:
            result[key] = value
    return result
