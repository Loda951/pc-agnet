from typing import cast

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import AgentRuntime
from app.agent.intent import classify_boundary
from app.api.routers.after_sales import create_after_sales_ticket
from app.core.config import Settings
from app.models import AppUser
from app.schemas.after_sales import CreateAfterSalesRequest


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("推荐 300 元以内无线鼠标", "in_scope_auto"),
        ("帮我查最近订单物流", "in_scope_auto"),
        ("退货政策怎么走", "in_scope_auto"),
        ("我要申请退货", "human_handoff_required"),
        ("帮我取消订单", "human_handoff_required"),
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
async def test_handoff_answer_uses_boundary_message_without_auto_workflow() -> None:
    boundary = classify_boundary("帮我创建售后工单")
    runtime = AgentRuntime(cast(AsyncSession, None), Settings(llm_api_key=""))
    state = {
        "message": "帮我创建售后工单",
        "intent": "after_sales",
        "boundary": boundary.model_dump(mode="json"),
        "parsed": {},
    }

    result = await runtime._generate(state)

    assert result["answer"] == boundary.display_message
    assert result["suggested_actions"] == [{"label": "转人工客服", "payload": {"handoff": True}}]


@pytest.mark.asyncio
async def test_after_sales_create_endpoint_is_downgraded_to_handoff() -> None:
    request = CreateAfterSalesRequest(
        order_id=1,
        order_item_id=1,
        ticket_type="return",
        reason="商品不符合预期",
    )

    with pytest.raises(HTTPException) as exc_info:
        await create_after_sales_ticket(
            request,
            AppUser(
                id=1,
                login_identifier="demo@example.com",
                display_name="Demo 用户",
                status="active",
            ),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["classification"] == "human_handoff_required"
