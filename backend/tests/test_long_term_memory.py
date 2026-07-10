from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppUser, MemoryFact
from app.repositories.conversations import ConversationRepository, utc_now_naive
from app.services.memory import MemoryService


def test_extract_long_term_facts_keeps_safe_preferences_and_filters_sensitive_text() -> None:
    service = MemoryService()

    facts = service.extract_long_term_facts(
        "我偏好无线鼠标，预算 500 元以内，手机号 13800138000，地址是测试路 1 号"
    )

    assert facts == [
        {
            "scope": "user",
            "fact_type": "preference",
            "key": "connection_preference",
            "value": "偏好无线设备",
            "confidence": 0.8,
        },
        {
            "scope": "user",
            "fact_type": "preference",
            "key": "budget_preference",
            "value": "偏好 500 元以内预算",
            "confidence": 0.7,
        },
    ]


@pytest.mark.asyncio
async def test_list_memory_filters_disabled_expired_and_other_users(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    now = utc_now_naive()
    async with db_session_factory() as session:
        session.add(
            AppUser(
                id=2,
                login_identifier="other-memory-user@example.com",
                display_name="Other Memory User",
                status="active",
            )
        )
        session.add_all(
            [
                MemoryFact(
                    user_id=1,
                    scope="user",
                    fact_type="preference",
                    key="connection_preference",
                    value="偏好无线设备",
                    confidence=0.8,
                ),
                MemoryFact(
                    user_id=1,
                    scope="user",
                    fact_type="preference",
                    key="disabled_preference",
                    value="不应读取",
                    confidence=0.8,
                    disabled_at=now,
                ),
                MemoryFact(
                    user_id=1,
                    scope="user",
                    fact_type="preference",
                    key="expired_preference",
                    value="不应读取",
                    confidence=0.8,
                    expires_at=now - timedelta(days=1),
                ),
                MemoryFact(
                    user_id=2,
                    scope="user",
                    fact_type="preference",
                    key="other_user_preference",
                    value="不应读取",
                    confidence=0.8,
                ),
            ]
        )
        await session.commit()

        memories = await ConversationRepository(session).list_memory(1)

    assert [(item.key, item.value) for item in memories] == [
        ("connection_preference", "偏好无线设备")
    ]


@pytest.mark.asyncio
async def test_upsert_memory_sets_governance_fields_and_can_disable_memory(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        repo = ConversationRepository(session)

        await repo.upsert_memory(
            1,
            "usage_preference",
            "偏好游戏场景",
            confidence=0.75,
            scope="user",
            fact_type="preference",
            source_message_id=123,
        )
        await session.commit()

        memories = await repo.list_memory(1)
        assert len(memories) == 1
        assert memories[0].scope == "user"
        assert memories[0].fact_type == "preference"
        assert memories[0].source_message_id == 123
        assert memories[0].last_used_at is not None

        disabled = await repo.disable_memory(1, memories[0].id)
        await session.commit()

        assert disabled is True
        assert await repo.list_memory(1) == []
