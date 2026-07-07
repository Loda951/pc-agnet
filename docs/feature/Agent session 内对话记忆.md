---
title: Agent session 内对话记忆
description: "记录 Agent 复用同一会话历史消息构造标准 chat format 上下文的实现范围、数据流和验证方式。"
tags: [feature, agent, session-memory, conversation, chat-format]
category: feature
doc_type: feature-summary
stage: phase-2
status: completed
priority: P1
---

# Agent session 内对话记忆

## 背景与目标

- 当前系统已经持久化 `conversation` 和 `message`，并有简单的用户长期偏好记忆 `memory_fact`。
- 但 Agent 调用 LLM 时只传入系统提示词、当前用户问题和检索上下文，没有按 `user -> assistant` 的一来一回顺序注入同一会话历史。
- 本 feature 的目标是补齐最小 session 内记忆：同一认证用户、同一 `conversation_id` 下的最近消息会以标准 chat role 形式进入 LLM 上下文。

## 实现范围

- 不新增数据库表，复用已有 `message` 表作为会话内短期记忆来源。
- 不修改 `MemoryFact` 长期记忆 schema，不在本次实现偏好撤销、过期、摘要或工作记忆表。
- 不改变 read-only 边界分类和售后人工接管策略。
- 不把跨用户或无权限会话历史注入上下文；`ConversationRepository.get_or_create()` 继续按 `user_id + conversation_id` 隔离。

## 关键变更

- `backend/app/repositories/conversations.py`
  - 新增 `list_recent_messages()`，按时间倒序读取最近消息后恢复为正序。
  - 只返回 `user` / `assistant` 消息，避免工具调用或其他内部角色进入 LLM chat history。
- `backend/app/agent/state.py`
  - `AgentState` 新增 `history` 字段，保存当前轮之前的会话消息。
- `backend/app/agent/graph.py`
  - `_load_context()` 在写入当前用户消息前读取最近 12 条历史。
  - `_llm_messages()` 将历史消息转换为 `HumanMessage` / `AIMessage`，并放在当前带检索上下文的问题之前。
- `backend/tests/test_session_memory.py`
  - 覆盖 LLM 消息构造必须包含历史 user/assistant。
  - 覆盖 `_load_context()` 从同一会话读取既有消息作为 session history。

## 数据流

1. `AgentRuntime._load_context()` 按当前认证用户获取或创建会话。
2. 写入当前用户消息前，读取该会话最近 12 条 `user` / `assistant` 消息。
3. 当前用户消息继续写入 `message` 表，并启动本轮 `agent_run`。
4. `AgentState.history` 保存历史消息的 `role` 和 `content`。
5. `_llm_messages()` 构造：
   - `SystemMessage(SYSTEM_PROMPT + BOUNDARY_PROTOCOL_PROMPT)`
   - 历史 `HumanMessage` / `AIMessage`
   - 当前 `HumanMessage("用户问题：...\\n检索上下文：...")`

## 方案决策

- 决策：先注入原始短历史，不做摘要。
  - 理由：当前最缺的是“上一轮说了什么”的基础能力；最近 12 条消息成本可控，且不需要新增异步摘要链路。
- 决策：当前问题仍携带结构化检索上下文。
  - 理由：商品、订单、RAG evidence 仍由当前轮工具检索产生，历史消息只补足指代和上下文承接。
- 决策：历史读取发生在写入当前用户消息前。
  - 理由：避免当前用户消息在 chat messages 中出现两次。

## 后续扩展

- 引入真正的工作记忆结构，保存最近商品候选、当前筛选条件、最近订单 ID、人工接管状态和最近 evidence。
- 为长期记忆增加 `scope`、`fact_type`、`expires_at`、`last_used_at`、`disabled_at` 等字段。
- 在前端展示“当前会话上下文”和“已记住偏好”的可撤销列表。

## 验证结果

- `cd backend && ./.venv/bin/pytest`

```text
35 passed, 11 skipped
```

- `cd backend && ./.venv/bin/ruff check .`

```text
All checks passed!
```

说明：当前本机 PostgreSQL 未启动，依赖数据库的集成测试按既有 fixture 策略跳过。
