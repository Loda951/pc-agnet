import importlib
import inspect
import json

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
