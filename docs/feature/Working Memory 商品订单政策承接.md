---
title: Working Memory 商品订单政策承接
description: "记录会话级 working memory：商品筛选承接、商品候选指代、订单承接、政策查询承接和人工接管草稿的实现范围、数据结构、Agent 数据流和验证结果。"
tags: [feature, working-memory, agent, 商品筛选, 订单承接, 政策查询]
category: feature
doc_type: feature-summary
stage: phase-2
status: completed
priority: P1
---

# Working Memory 商品订单政策承接

## 背景与目标

- Session history 已能把同一会话最近 `user` / `assistant` 原文按 chat format 注入 LLM。
- 但只靠原文历史不适合稳定解决“换成无线”“这个订单”“那保修呢”这类需要结构化状态的多轮承接。
- 本 feature 的目标是新增会话级 working memory，优先覆盖：
  - 商品筛选承接
  - 商品候选指代
  - 订单承接
  - 政策查询承接
  - 人工接管草稿预填

## 实现范围

- 新增 `conversation.working_memory_json` JSONB 字段，用于保存同一会话内结构化状态。
- 新增 `MemoryService`，把 working memory 的读取、意图承接、检索参数合并和回写逻辑集中在一个深模块。
- 继续使用认证后的 `current_user.id` 和 `conversation_id` 做隔离，不接受请求体 `user_id`。
- 不在本次升级长期 `MemoryFact` schema，不做用户级长期偏好撤销或过期。
- 不改变 read-only 边界；订单售后办理仍只进入人工接管，不自动执行退款、退货、维修或订单修改。

## Working Memory 结构

示例：

```json
{
  "current_product_search": {
    "query": "鼠标",
    "category": "鼠标",
    "max_price": "500",
    "filters": {
      "connection_type": "Wireless"
    },
    "limit": 6
  },
  "recent_products": [
    {
      "spu_id": 1,
      "sku_id": 10,
      "title": "Razer Codex Viper V3 Pro White",
      "category": "鼠标",
      "price": "139.99",
      "stock": 8,
      "specs": {
        "connection_type": "Wired, Wireless"
      }
    }
  ],
  "last_referenced_product": {
    "sku_id": 10,
    "title": "Razer Codex Viper V3 Pro White",
    "category": "鼠标",
    "price": "139.99",
    "stock": 8,
    "specs": {
      "connection_type": "Wired, Wireless"
    }
  },
  "last_order_id": 202607020001,
  "last_policy_query": "退货政策怎么走",
  "recent_evidence": [
    {
      "source_type": "knowledge_document",
      "source_id": 9001,
      "title": "测试退货政策",
      "document_type": "policy"
    }
  ],
  "pending_handoff": {
    "order_id": 202607020001,
    "request_type": "return",
    "reason": "这个订单要退货"
  }
}
```

## Agent 数据流

1. `AgentRuntime._load_context()` 读取 session history、长期偏好记忆和 `working_memory_json`。
2. `MemoryService.resolve_intent()` 识别商品筛选、政策查询等 follow-up，将短追问从 `general` 纠正到业务意图。
3. `MemoryService.resolve_product_search()` 将“换成无线”合并到上一轮商品筛选条件。
4. `MemoryService.resolve_referenced_product()` 将“第二个”“这款”等表达解析到 `recent_products` 中的具体 SKU。
5. `MemoryService.resolve_order_id()` 将“这个订单”解析为上一轮 `last_order_id`。
6. `MemoryService.resolve_knowledge_query()` 将短政策追问和上一轮政策查询拼成更完整的知识检索 query。
7. `_suggest_actions()` 对人工接管建议动作补充 `orderId`、`requestType` 和 `reason`，供前端预填。
8. `_llm_messages()` 把 `working_memory` 和 `parsed` 放入检索上下文，让 LLM 能看到结构化会话状态和当前指代解析结果。
9. `_persist()` 在每轮结束时根据商品、订单、evidence、商品指代和人工接管边界回写 `working_memory_json`。

## 验证结果

- `cd backend && ./.venv/bin/pytest tests/test_working_memory.py -q`

```text
7 passed
```

- `cd backend && ./.venv/bin/pytest`

```text
42 passed, 11 skipped
```

- `cd backend && ./.venv/bin/ruff check .`

```text
All checks passed!
```

- `cd backend && ./.venv/bin/alembic upgrade head --sql`

```text
ALTER TABLE conversation ADD COLUMN working_memory_json JSONB;
```

说明：当前本机 PostgreSQL 未启动，依赖数据库的集成测试按既有 fixture 策略跳过。

## 后续扩展

- 商品候选指代继续扩展到“和上一款比”“有没有比第三个便宜一点的”等对比型追问。
- 前端人工接管面板消费建议动作中的 `orderId`、`requestType` 和 `reason`，减少用户重复输入。
- 升级长期 `MemoryFact`，加入 `scope`、`fact_type`、`expires_at`、`last_used_at` 和 `disabled_at`。
- 前端展示“当前会话上下文”和“已记住偏好”的可撤销列表。
