from datetime import datetime
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import AgentRuntime
from app.agent.intent import classify_boundary
from app.api.routers import after_sales as after_sales_router
from app.core.config import Settings
from app.models import AppUser
from app.schemas.after_sales import CreateAfterSalesRequest, HandoffRequestCard


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("推荐 300 元以内无线鼠标", "in_scope_auto"),
        ("帮我查最近订单物流", "in_scope_auto"),
        ("退货政策怎么走", "in_scope_auto"),
        ("怎么下单购买键盘", "in_scope_auto"),
        ("支持哪些支付方式", "in_scope_auto"),
        ("我要申请退货", "human_handoff_required"),
        ("帮我取消订单", "human_handoff_required"),
        ("帮我下单这个鼠标", "human_handoff_required"),
        ("帮我支付这个订单", "human_handoff_required"),
        ("帮我写一段 Python 爬虫", "out_of_scope"),
        ("推荐一台手机", "out_of_scope"),
    ],
)
def test_classifies_read_only_boundary(message: str, expected: str) -> None:
    boundary = classify_boundary(message)

    assert boundary.classification == expected
    assert boundary.reason
    assert boundary.display_message


@pytest.mark.asyncio
async def test_handoff_answer_uses_boundary_template() -> None:
    boundary = classify_boundary("帮我创建售后工单")
    runtime = AgentRuntime(cast(AsyncSession, None), Settings(llm_api_key=""))
    state = {
        "message": "帮我创建售后工单",
        "intent": "after_sales",
        "boundary": boundary.model_dump(mode="json"),
        "parsed": {},
    }

    result = await runtime._render_handoff_template(state)

    assert result["answer"] == boundary.display_message
    assert result["suggested_actions"] == [
        {
            "label": "转人工客服",
            "payload": {
                "handoff": True,
                "orderId": None,
                "requestType": "other",
                "reason": "帮我创建售后工单",
            },
        }
    ]


@pytest.mark.asyncio
async def test_purchase_guidance_direct_response_explains_read_only_order_flow() -> None:
    runtime = AgentRuntime(cast(AsyncSession, None), Settings(llm_api_key=""))
    state = {
        "message": "怎么下单购买键盘",
        "tool_results": [],
    }

    decision = runtime._fallback_orchestrator_decision(state)

    assert decision.type == "direct_response"
    assert decision.response.startswith("下单流程可以按这几步走")
    assert "我不能在聊天中替你提交订单或完成支付" in decision.response


@pytest.mark.asyncio
async def test_after_sales_create_endpoint_records_pending_handoff_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHandoffRequestRepository:
        def __init__(self, session: AsyncSession):
            self.session = session

        async def create_request(
            self,
            user_id: int,
            request: CreateAfterSalesRequest,
            boundary_category: str,
        ):
            assert user_id == 1
            assert request.session_id == 99
            assert request.request_type == "return"
            assert boundary_category == "human_handoff_required"
            return HandoffRequestCard(
                id=876,
                session_id=request.session_id,
                order_id=request.order_id,
                request_type=request.request_type,
                reason=request.reason,
                boundary_category=boundary_category,
                status="pending",
                created_at=datetime(2026, 7, 4, 12, 0, 0),
                updated_at=datetime(2026, 7, 4, 12, 0, 0),
            )

    monkeypatch.setattr(
        after_sales_router,
        "HandoffRequestRepository",
        FakeHandoffRequestRepository,
        raising=False,
    )

    response = await after_sales_router.create_after_sales_ticket(
        CreateAfterSalesRequest(
            session_id=99,
            order_id=None,
            request_type="return",
            reason="商品不符合预期",
        ),
        AppUser(
            id=1,
            login_identifier="demo@example.com",
            display_name="Demo 用户",
            status="active",
        ),
        cast(AsyncSession, None),
    )

    assert response.request_id == 876
    assert response.status == "pending"
    assert "不会自动办理业务操作" in response.message
