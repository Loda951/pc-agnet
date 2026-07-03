from collections.abc import AsyncIterator, Callable

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import AgentRuntime
from app.api.routers import chat as chat_router
from app.core.config import Settings, get_settings
from app.core.database import get_session
from app.main import app
from app.schemas.chat import EvidenceItem


class FakeKnowledgeService:
    async def retrieve(self, query: str) -> list[EvidenceItem]:
        if "退货" not in query:
            return []
        return [
            EvidenceItem(
                source_type="knowledge_document",
                source_id=9001,
                title="测试退货政策",
                document_type="policy",
                snippet="签收次日起七天内，未影响二次销售可申请七天无理由退货。",
                score=0.91,
                metadata={"scenario": "return"},
            )
        ]


class RuntimeWithFakeKnowledge(AgentRuntime):
    def __init__(self, session: AsyncSession, settings: Settings):
        super().__init__(session, settings, knowledge_service=FakeKnowledgeService())


@pytest_asyncio.fixture
async def api_client(
    db_session_factory: Callable[[], AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with db_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_settings] = lambda: Settings(llm_api_key="", default_user_id=1)
    monkeypatch.setattr(chat_router, "AgentRuntime", RuntimeWithFakeKnowledge)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_chat_recommends_real_dataset_wireless_mouse(api_client: AsyncClient) -> None:
    response = await api_client.post(
        "/api/chat",
        json={"message": "推荐 1200 元以内 Codex 无线鼠标"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["boundary"]["classification"] == "in_scope_auto"
    assert payload["intent"] == "product_recommendation"
    assert payload["products"][0]["title"] == "Razer Codex Viper V3 Pro White"
    assert "Wireless" in payload["products"][0]["specs"]["connection_type"]
    assert "兼容" in payload["answer"] or "适合" in payload["answer"]


@pytest.mark.asyncio
async def test_chat_returns_rag_evidence_for_after_sales_policy(
    api_client: AsyncClient,
) -> None:
    response = await api_client.post("/api/chat", json={"message": "退货政策怎么走"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["boundary"]["classification"] == "in_scope_auto"
    assert payload["intent"] == "after_sales"
    assert payload["evidence"][0]["title"] == "测试退货政策"
    assert "测试退货政策" in payload["answer"]


@pytest.mark.asyncio
async def test_orders_latest_returns_seeded_order(api_client: AsyncClient) -> None:
    response = await api_client.get("/api/orders/latest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status_label"] == "已发货"
    assert payload["items"]
    assert payload["logistics"]["logistic_no"] == "SF100200300400"


@pytest.mark.asyncio
async def test_after_sales_endpoint_returns_handoff_boundary(api_client: AsyncClient) -> None:
    response = await api_client.post(
        "/api/after-sales",
        json={
            "order_id": 202607020001,
            "order_item_id": 1,
            "ticket_type": "return",
            "reason": "商品不符合预期",
        },
    )

    assert response.status_code == 409
    payload = response.json()
    assert payload["detail"]["classification"] == "human_handoff_required"
