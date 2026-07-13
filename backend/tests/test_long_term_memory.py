from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppUser, MemoryFact
from app.repositories.conversations import ConversationRepository, utc_now_naive
from app.services.memory import MemoryService


def test_one_turn_request_does_not_create_long_term_facts() -> None:
    service = MemoryService()

    facts = service.extract_long_term_facts(
        "我偏好无线鼠标，预算 500 元以内，手机号 13800138000，地址是测试路 1 号"
    )

    assert facts == []


def test_extract_long_term_facts_accepts_explicit_stable_preferences() -> None:
    service = MemoryService()

    facts = service.extract_long_term_facts("请记住，我通常偏好无线鼠标，长期预算 500 元以内")

    assert facts == [
        {
            "scope": "user",
            "fact_type": "preference",
            "key": "connection_preference",
            "value": "偏好无线设备",
            "value_json": {
                "preference": "wireless",
                "negated": False,
                "operation": "set",
            },
            "confidence": 0.8,
        },
        {
            "scope": "user",
            "fact_type": "preference",
            "key": "budget_preference",
            "value": "偏好 500 元以内预算",
            "value_json": {
                "amount": 500.0,
                "currency": "CNY",
                "maximum": True,
                "operation": "set",
            },
            "confidence": 0.7,
        },
    ]


def test_extract_long_term_facts_preserves_negation_as_structured_correction() -> None:
    service = MemoryService()

    facts = service.extract_long_term_facts("以后不要无线")

    assert facts == [
        {
            "scope": "user",
            "fact_type": "preference",
            "key": "connection_preference",
            "value": "不偏好无线设备",
            "value_json": {
                "preference": "wireless",
                "negated": True,
                "operation": "exclude",
            },
            "confidence": 0.8,
        }
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
                    value_json={"preference": "wireless", "negated": False},
                    origin="explicit_user",
                    confidence=0.8,
                ),
                MemoryFact(
                    user_id=1,
                    scope="user",
                    fact_type="preference",
                    key="legacy_preference",
                    value="不应读取",
                    value_json={"value": "legacy"},
                    origin="legacy_inferred",
                    confidence=0.8,
                ),
                MemoryFact(
                    user_id=1,
                    scope="user",
                    fact_type="preference",
                    key="disabled_preference",
                    value="不应读取",
                    value_json={"value": "disabled"},
                    origin="explicit_user",
                    confidence=0.8,
                    disabled_at=now,
                ),
                MemoryFact(
                    user_id=1,
                    scope="user",
                    fact_type="preference",
                    key="expired_preference",
                    value="不应读取",
                    value_json={"value": "expired"},
                    origin="explicit_user",
                    confidence=0.8,
                    expires_at=now - timedelta(days=1),
                ),
                MemoryFact(
                    user_id=2,
                    scope="user",
                    fact_type="preference",
                    key="other_user_preference",
                    value="不应读取",
                    value_json={"value": "other-user"},
                    origin="explicit_user",
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
        untouched = await repo.upsert_memory(
            1,
            "brand_preference",
            "偏好罗技品牌",
            value_json={"brand": "罗技", "negated": False},
        )
        await session.commit()

        memories = await repo.list_memory(1)
        usage_memory = next(item for item in memories if item.key == "usage_preference")
        assert len(memories) == 2
        assert usage_memory.scope == "user"
        assert usage_memory.fact_type == "preference"
        assert usage_memory.source_message_id == 123
        assert usage_memory.origin == "explicit_user"
        assert usage_memory.value_json == {"value": "偏好游戏场景"}
        assert usage_memory.last_used_at is None

        marked = await repo.mark_memory_used(1, [usage_memory.id])
        await session.commit()
        assert marked == 1
        assert usage_memory.last_used_at is not None
        assert untouched.last_used_at is None

        disabled = await repo.disable_memory(1, usage_memory.id)
        await session.commit()

        assert disabled is True
        remaining = await repo.list_memory(1)
        assert [item.key for item in remaining] == ["brand_preference"]
