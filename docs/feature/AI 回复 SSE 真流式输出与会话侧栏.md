---
title: AI 回复 SSE 进度流与会话侧栏
description: "记录第二阶段前端工作台中的 SSE 进度事件、完整回复校验、会话列表隔离、订单可展开明细、取消超时重试体验和验证结果。"
tags: [feature, SSE, streaming, chat, 会话隔离, 前端工作台, 订单明细]
category: feature
doc_type: feature-summary
stage: phase-2
status: superseded
priority: P0
---

# AI 回复 SSE 进度流与会话侧栏

## 当前决策（2026-07-18）

- 本文以下内容保留为历史实现记录，不再代表前端默认聊天链路。
- 前端已切换到 `sendChat()` -> `POST /api/chat`，等待 `AgentRuntime.run()` 返回完整 `ChatResponse`。
- LLM 继续使用 `streaming=False` 和完整 `AIMessage`；不解析或展示 token/chunk 增量。
- `/api/chat/stream` 与 `AgentRuntime.run_stream()` 暂时保留为兼容入口，但前端不再调用。
- 后续若确认没有外部消费者，可以单独删除 SSE router、事件类型和解析代码。

## 背景与目标

- 此前 `/api/chat/stream` 会先等待 `AgentRuntime.run()` 生成完整回答，再把 answer 拆行发送；用户体验仍然是“等完整回答”。
- 第二阶段需要把边界判断、工具检索和上下文更新逐步暴露给前端；最终回答在完整生成并校验后一次性发送。
- 前端左侧从快捷 prompt 调整为当前用户的会话列表，让每次对话可切换、可隔离、可恢复。
- 订单上下文需要可点开查看明细，避免只看到一行摘要。

## 关键变更清单

### 后端流式 Agent

- `backend/app/agent/graph.py`
  - 新增 `AgentRuntime.run_stream()`，顺序复用现有节点逻辑，不影响原 `/api/chat` 的一次性 `run()`。
  - 事件类型覆盖：
    - `run_started`
    - `boundary`
    - `tool_call`
    - `context`
    - `delta`
    - `done`
    - `error`
  - 商品、订单、知识库检索会在开始/完成时发送 `tool_call`，并在上下文变化后发送 `context`。
  - LLM 路径使用 `ainvoke()` 获取完整响应；原生 Tool Call 继续进入 loop，没有 Tool Call 的完整正文直接进入 `finalize_response`。
  - 终态不再使用 `TYPE:` 头；已有成功 Tool Result 时，普通模型正文仅在内部按 `grounded_response` 记录。
  - 客户端断开时将 run 标记为 failed/cancelled，避免长期停留在 running。

- `backend/app/core/llm.py`
  - `ChatOpenAI` 显式使用 `streaming=False`，避免未校验的部分正文进入用户界面。

- `backend/app/api/routers/chat.py`
  - `/api/chat/stream` 改为直接消费 `run_stream()`。
  - SSE 使用 `event:` + JSON `data:`，并设置 `Cache-Control: no-cache`、`X-Accel-Buffering: no`。

### 会话列表与隔离

- 新增 `backend/app/api/routers/conversations.py`：
  - `GET /api/conversations`：按当前认证用户列出会话。
  - `GET /api/conversations/{conversation_id}`：按当前认证用户读取会话消息。
  - 不接受公开 `user_id`，用户 B 读取用户 A 会话返回 404。

- `backend/app/repositories/conversations.py`
  - 新增会话列表和详情读取方法。
  - 新会话标题使用第一条用户消息截断生成。
  - 新消息写入时更新 `conversation.updated_at`。

### 前端流式体验

- `frontend/src/api.ts`
  - 新增 `sendChatStream()`，使用 `fetch` + `ReadableStreamDefaultReader` 解析 POST SSE。
  - 支持 access token 自动刷新。
  - 支持流式断开检测、60 秒无事件超时、用户取消和 retryable error。

- `frontend/src/App.tsx`
  - 发送消息后立即插入生成中的助手气泡。
  - `boundary` 事件更新顶部边界和气泡徽标。
  - `tool_call` 事件更新生成阶段文案。
  - `context` 事件即时更新右侧商品、订单、evidence 面板。
  - `delta` 事件一次性写入已经完整校验的助手回答。
  - `done` 事件补齐最终 metadata、suggested actions 和会话记录。
  - 取消流式请求会显示“已取消”，超时/断流会保留重试入口。

### 左侧会话与订单明细

- `frontend/src/components/Sidebar.tsx`
  - 左栏从快捷请求改为用户会话列表。
  - 支持新建会话、选择历史会话、展示最近消息和时间。

- `frontend/src/components/ContextPanel.tsx`
  - 订单卡支持点击展开。
  - 展开后展示商品明细、规格、金额数量和物流轨迹。

## 方案与决策

- 决策：保留原 `/api/chat` 和 LangGraph `run()`，新增顺序版 `run_stream()`。
  - 理由：非流式接口和既有测试保持稳定；流式路径更容易精确控制事件时机。

- 决策：保留 SSE 进度事件，但最终模型回答不做 token 流式输出。
  - 理由：客服回答需要先完整生成；未完成的部分正文一旦发送便无法撤回。完整正文直接在 `finalize_response` 后一次性发送。

- 决策：前端使用 `fetch reader`，不使用 `EventSource`。
  - 理由：聊天请求需要 POST body 和 Authorization header，`EventSource` 不适合该场景。

- 决策：会话列表读取持久化消息 metadata 恢复右侧上下文。
  - 理由：不需要额外查询商品或订单即可恢复用户上次看到的依据、商品和订单上下文。

## 验证结果

- 后端相关测试：

```text
13 passed, 8 skipped
```

- 后端完整测试：

```text
26 passed, 10 skipped
```

- 后端 lint：

```text
ruff check . -> All checks passed!
```

- 前端构建：

```text
npm run build -> passed
```

## 遗留事项

- 当前会话列表只支持按最近更新时间排序，尚未支持重命名、删除、置顶和搜索。
- 订单“可点开”先实现为右侧上下文内展开明细，尚未引入独立前端路由页面。
- SSE 事件目前覆盖核心进度，后续可增加更细的检索耗时和重排分数；最终正文保持完整校验后发送。
