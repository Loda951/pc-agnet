import importlib
import inspect
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import Settings


def _context_module():
    return importlib.import_module("app.services.context")


def _context_schema_module():
    return importlib.import_module("app.schemas.context")


def test_context_service_exposes_async_turn_boundary() -> None:
    service_type = _context_module().ConversationContextService

    assert inspect.iscoroutinefunction(service_type.prepare_turn)
    assert inspect.iscoroutinefunction(service_type.complete_turn)


def test_history_selection_keeps_at_most_six_complete_recent_turns() -> None:
    context = _context_module()
    messages = [
        item
        for turn in range(7)
        for item in (
            {"role": "user", "content": f"user-{turn}"},
            {"role": "assistant", "content": f"assistant-{turn}"},
        )
    ]
    messages.append({"role": "user", "content": "failed-unmatched-user-message"})

    selection = context.select_complete_turns(messages, budget_tokens=10_000)

    assert [(item.role, item.content) for item in selection.messages] == [
        (role, f"{role}-{turn}")
        for turn in range(1, 7)
        for role in ("user", "assistant")
    ]
    assert selection.retained_turns == 6
    assert selection.dropped_turns == 1
    assert all(item.content != "failed-unmatched-user-message" for item in selection.messages)


def test_history_selection_obeys_deterministic_token_budget() -> None:
    context = _context_module()
    messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
    ]
    newest_pair_budget = sum(
        context.estimate_message_tokens(value) for value in ("u3", "a3")
    )

    first = context.select_complete_turns(messages, budget_tokens=newest_pair_budget)
    second = context.select_complete_turns(messages, budget_tokens=newest_pair_budget)

    assert first == second
    assert [(item.role, item.content) for item in first.messages] == [
        ("user", "u3"),
        ("assistant", "a3"),
    ]
    assert first.estimated_token_count == newest_pair_budget
    assert first.retained_turns == 1
    assert first.dropped_turns == 2


def test_v1_working_memory_upgrades_to_typed_v2_without_volatile_values() -> None:
    schemas = _context_schema_module()
    legacy = {
        "current_product_search": {
            "query": "无线鼠标",
            "category": "鼠标",
            "max_price": "500",
            "filters": {"connection_type": "Wireless"},
        },
        "recent_products": [
            {
                "spu_id": 10,
                "sku_id": 101,
                "title": "Example Mouse",
                "price": "199.00",
                "stock": 5,
                "specs": {"dpi": 12000},
            }
        ],
        "last_referenced_product": {
            "spu_id": 10,
            "sku_id": 101,
            "price": "199.00",
            "stock": 5,
        },
        "last_order_id": 202607020001,
        "last_policy_query": "退货政策",
        "recent_evidence": [
            {
                "source_type": "knowledge_document",
                "source_id": 9001,
                "title": "退货政策",
                "document_type": "policy",
                "snippet": "完整证据正文不应持久化",
            }
        ],
        "pending_handoff": {
            "order_id": 202607020001,
            "request_type": "return",
            "reason": "需要退货",
        },
        "logistics": {"carrier": "do-not-persist"},
    }

    memory = schemas.upgrade_working_memory(legacy)
    payload = memory.model_dump(mode="json", exclude_none=True)
    serialized = json.dumps(payload, ensure_ascii=False)

    assert memory.schema_version == 2
    assert memory.catalog.query_plan["query"] == "无线鼠标"
    assert memory.catalog.candidate_spu_ids == [10]
    assert memory.catalog.candidate_sku_ids == [101]
    assert memory.catalog.referenced_sku_id == 101
    assert memory.order.last_order_id == 202607020001
    assert memory.policy.evidence_refs[0].source_id == 9001
    assert memory.handoff.request_type == "return"
    for forbidden in ("199.00", "stock", "specs", "完整证据正文", "carrier"):
        assert forbidden not in serialized


def test_v2_query_plan_keeps_only_supported_constraint_fields() -> None:
    schemas = _context_schema_module()
    memory = schemas.WorkingMemoryV2.model_validate(
        {
            "catalog": {
                "query_plan": {
                    "query": "无线鼠标",
                    "category": "mouse",
                    "min_price": 100,
                    "max_price": 500,
                    "filters": {
                        "connection_type": "Wireless",
                        "stock": 5,
                        "specs": {"dpi": 12000},
                        "logistics": {"carrier": "secret"},
                    },
                    "sort": "recommend",
                    "limit": 3,
                    "sale_price": 199,
                    "inventory": 5,
                    "shipping_details": {"carrier": "secret"},
                    "excerpt": "secret",
                    "specs": {"dpi": 12000},
                    "snippet": "secret",
                }
            }
        }
    )

    assert memory.catalog.query_plan == {
        "query": "无线鼠标",
        "category": "mouse",
        "min_price": 100,
        "max_price": 500,
        "filters": {"connection_type": "Wireless"},
        "sort": "recommend",
        "limit": 3,
    }


def test_compact_audit_omits_context_payloads_and_keeps_metrics() -> None:
    context = _context_module()
    audit = context.serialize_compact_audit(
        {
            "intent": "product_recommendation",
            "history": [{"role": "user", "content": "secret history"}],
            "memory": [{"key": "preference", "value": "secret memory"}],
            "working_memory": {"catalog": {"candidate_sku_ids": [101]}},
        },
        estimated_token_count=42,
        retained_turns=2,
        dropped_turns=3,
        applied_memory_ids=[7, 9],
    )

    assert audit == {
        "intent": "product_recommendation",
        "estimated_token_count": 42,
        "retained_turns": 2,
        "dropped_turns": 3,
        "applied_memory_ids": [7, 9],
    }


def test_context_budget_defaults_to_6000_and_reads_env(monkeypatch) -> None:
    assert Settings().agent_context_budget_tokens == 6000

    monkeypatch.setenv("AGENT_CONTEXT_BUDGET_TOKENS", "321")

    assert Settings().agent_context_budget_tokens == 321


@pytest.mark.asyncio
async def test_prepare_and_complete_turn_own_context_persistence_and_audit() -> None:
    context = _context_module()
    memory = SimpleNamespace(
        id=7,
        scope="user",
        fact_type="preference",
        key="connection_preference",
        value="偏好无线设备",
        value_json={"preference": "wireless", "operation": "set"},
        confidence=0.8,
    )
    repository = FakeContextRepository(memory)
    session = FakeContextSession(repository.run)
    service = context.ConversationContextService(
        session,
        Settings(llm_api_key="", agent_context_budget_tokens=6000),
        repository=repository,
    )

    prepared = await service.prepare_turn(1, None, "以后不要无线")
    changes = await service.complete_turn(
        prepared,
        {
            "message": prepared.message,
            "answer": "已记录",
            "intent": "product_recommendation",
            "boundary": {"classification": "in_scope_auto"},
            "applied_memory_ids": ["invalid", True, 7.9, "7", 999, 7],
            "products": [
                SimpleNamespace(
                    spu_id=10,
                    sku_id=101,
                    title="Test Mouse",
                    brand="Razer",
                    category="mouse",
                    image_url="/mouse.png",
                    price="199",
                    stock=5,
                    specs={},
                )
            ],
        },
    )

    assert prepared.retained_turns == 1
    assert repository.messages[-1][1:] == ("assistant", "已记录")
    assert repository.upserts[0]["value_json"]["operation"] == "exclude"
    assert repository.marked_ids == [7]
    assert repository.updated_working_memory["schema_version"] == 2
    assert repository.updated_working_memory["catalog"]["candidate_display"] == [
        {
            "spu_id": 10,
            "sku_id": 101,
            "title": "Test Mouse",
            "brand": "Razer",
            "category": "mouse",
            "image_url": "/mouse.png",
        }
    ]
    serialized = json.dumps(repository.updated_working_memory, ensure_ascii=False)
    assert "199" not in serialized
    assert "stock" not in serialized
    assert "specs" not in serialized
    assert repository.finished_state == changes.audit
    assert "history" not in changes.audit
    assert "memory" not in changes.audit
    assert "working_memory" not in changes.audit
    assert changes.applied_memory_ids == [7]
    assert [item.model_dump(mode="json") for item in changes.memory_changes] == [
        {
            "action": "updated",
            "memory_id": 88,
            "key": "connection_preference",
            "display_value": "不偏好无线设备",
        }
    ]
    assert session.committed is True


def test_memory_upsert_statement_is_atomic_for_active_identity() -> None:
    repositories = importlib.import_module("app.repositories.conversations")
    statement = repositories._memory_upsert_statement(
        user_id=1,
        key="brand_preference",
        value="偏好 罗技 品牌",
        confidence=0.8,
        scope="user",
        fact_type="preference",
        value_json={"brand": "罗技"},
        source_message_id=9,
        expires_at=None,
        now=SimpleNamespace(),
    )
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "ON CONFLICT" in sql
    assert "disabled_at IS NULL" in sql
    assert "xmax = 0" in sql


@pytest.mark.asyncio
async def test_memory_change_action_comes_from_atomic_upsert_result() -> None:
    context = _context_module()
    existing = SimpleNamespace(
        id=7,
        scope="user",
        fact_type="preference",
        key="connection_preference",
        value="偏好无线设备",
        value_json={"preference": "wireless"},
        confidence=0.8,
    )
    repository = FakeContextRepository(existing, upsert_created=True)
    session = FakeContextSession(repository.run)
    service = context.ConversationContextService(
        session,
        Settings(llm_api_key=""),
        repository=repository,
    )

    prepared = await service.prepare_turn(1, None, "以后不要无线")
    changes = await service.complete_turn(
        prepared,
        {
            "answer": "已记录",
            "intent": "product_recommendation",
            "boundary": {"classification": "in_scope_auto"},
        },
    )

    assert changes.memory_changes[0].action == "created"


def test_applied_memory_ids_reject_booleans_and_fractional_numbers() -> None:
    context = _context_module()

    assert context._valid_memory_ids([True, 7.9], {1, 7}) == []


def test_successful_empty_catalog_result_clears_candidates_but_failure_preserves_them() -> None:
    context = _context_module()
    schemas = _context_schema_module()
    previous = schemas.WorkingMemoryV2.model_validate(
        {
            "catalog": {
                "query_plan": {"query": "old mouse"},
                "candidate_spu_ids": [10],
                "candidate_sku_ids": [101],
                "candidate_display": [{"spu_id": 10, "sku_id": 101, "title": "Old"}],
            }
        }
    )

    success = context._next_working_memory(
        previous,
        {
            "parsed": {"product_search": {"query": "no match"}},
            "products": [],
            "catalog_tool_succeeded": True,
        },
    )
    failure = context._next_working_memory(
        previous,
        {
            "products": [],
            "catalog_tool_succeeded": False,
        },
    )

    assert success.catalog.candidate_spu_ids == []
    assert success.catalog.candidate_sku_ids == []
    assert success.catalog.candidate_display == []
    assert failure.catalog.candidate_sku_ids == [101]


class FakeContextRepository:
    def __init__(self, memory, *, upsert_created: bool = False) -> None:
        self.memory = memory
        self.upsert_created = upsert_created
        self.run = SimpleNamespace(id=44)
        self.messages: list[tuple[int, str, str]] = []
        self.upserts: list[dict] = []
        self.marked_ids: list[int] = []
        self.updated_working_memory: dict = {}
        self.finished_state: dict = {}

    async def get_or_create(self, _user_id: int, _conversation_id: int | None):
        return SimpleNamespace(id=33)

    async def list_recent_messages(self, _conversation_id: int, limit: int):
        assert limit == 64
        return [
            SimpleNamespace(role="user", content="上一轮"),
            SimpleNamespace(role="assistant", content="上一轮回答"),
            SimpleNamespace(role="user", content="失败消息"),
        ]

    async def get_working_memory(self, _conversation_id: int):
        return {"recent_products": [{"spu_id": 9, "sku_id": 99, "price": "99"}]}

    async def list_memory(self, _user_id: int):
        return [self.memory]

    async def add_message(self, conversation_id: int, role: str, content: str, metadata=None):
        self.messages.append((conversation_id, role, content))
        return SimpleNamespace(id=len(self.messages))

    async def start_run(self, _conversation_id: int):
        return self.run

    async def upsert_memory(
        self,
        _user_id: int,
        _key: str,
        _value: str,
        _confidence: float,
        **kwargs,
    ):
        self.upserts.append(kwargs)
        return SimpleNamespace(
            memory=SimpleNamespace(id=88),
            created=self.upsert_created,
        )

    async def update_working_memory(self, _conversation_id: int, value: dict):
        self.updated_working_memory = value

    async def mark_memory_used(self, _user_id: int, memory_ids: list[int]):
        self.marked_ids = memory_ids
        return len(memory_ids)

    async def finish_run(self, _run, _intent: str, state: dict):
        self.finished_state = state


class FakeContextSession:
    def __init__(self, run) -> None:
        self.run = run
        self.committed = False

    async def get(self, _model, _run_id: int):
        return self.run

    async def commit(self):
        self.committed = True
