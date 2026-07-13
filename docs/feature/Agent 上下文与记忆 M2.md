---
title: Agent 上下文与记忆 M2
description: "记录 typed 会话上下文、token 预算、ToolRegistry 主链、显式长期偏好治理和用户记忆控制面的实现。"
tags: [feature, agent, context, memory, tool-registry, working-memory]
category: feature
doc_type: implementation
stage: phase-2
status: completed
priority: P1
---

# Agent 上下文与记忆 M2

## 目标与结论

- 将 session history、working memory 和长期偏好收敛到 `ConversationContextService`，主流程通过 `prepare_turn()` 与 `complete_turn()` 使用上下文。
- `AgentRuntime` 已通过 `ToolRegistry` 调用 `catalog.search`、`catalog.compare` 和 `order.lookup`；知识检索仍使用现有 `ChromaKnowledgeService`。
- 当前不引入 mem0、向量化用户记忆或 LLM 会话摘要。

## 上下文结构

- `WorkingMemoryV2` 固定 `schema_version=2`，分为 catalog、order、policy、handoff 四个 typed 子状态。
- catalog 将稳定 search query plan 与最近 comparison metadata 分字段保存，同时保留 SKU/SPU ID 和展示身份；order 只保存订单 ID；policy 只保存 query 和 evidence 引用。
- 价格、库存、规格快照、物流详情和 evidence 正文不进入 working memory；追问时重新调用工具读取当前事实。
- V1 JSON 在读取时转换为 V2，并在下一次成功完成对话时回写。

## Session history 与 token 预算

- `AGENT_CONTEXT_BUDGET_TOKENS` 默认 `6000`。
- 最多保留最近 6 个完整 `user -> assistant` 轮次，按确定性 token 估算从新到旧选择。
- 未完成或失败后遗留的单边用户消息不进入 LLM history。
- 裁剪结果记录 estimated token、保留轮数和丢弃轮数，不生成 LLM 摘要；丢弃轮数通过完整 assistant 轮次计数校准，不受最近 64 条消息读取窗口影响。

## 工具编排与优先级

- 上下文优先级为：当前请求和当前工具事实 > working memory > 显式长期偏好 > 最近完整历史。
- 商品追问从完整安全 V2 search query plan 出发，再合并当前显式覆盖；query、价格区间、允许的 filters、keywords、sort、category、brand 和 usage 都会保留，短追问文本不会成为 repository keyword。显式否定品牌会从历史正向品牌中移除；切换品类时使用当前 query，并清除旧品类的品牌、filters、keywords、usage 和连接方式默认值。
- “第一个/第二个/这些商品对比”解析为 working memory 中的 SKU ID，再调用 `catalog.compare` 刷新事实；comparison metadata 不覆盖稳定 search query plan，直接 SKU 对比只返回 active Sku 与 active Spu。
- `order.lookup.user_id` 只来自认证后的 Agent state。
- 正向和负向偏好都使用 typed defaults/exclusions；当前显式条件覆盖 working 和长期偏好。
- 品牌排除直接下推 SQL；用途排除保留原首批候选窗口（`min(max(limit*50, 100), 1000)`），不足时按同一窗口有界分页，直到拿满、数据库耗尽或达到 50 页安全上限，再应用最终 limit。
- ToolRegistry 失败会记录 tool call 并安全降级；成功空结果清空旧商品候选，失败则保留旧 working state。

## 长期偏好治理

- 只从“以后、长期、我通常、请记住、记住我”等显式稳定表达抽取长期偏好。
- 普通“预算 500、要无线”只影响当前会话；“以后不要无线/不要 Logitech/不玩游戏”保存为结构化 exclusion。
- `memory_fact.value_json` 保存结构化值，`origin=explicit_user` 才能进入个性化上下文；旧数据标记为 `legacy_inferred`。
- 活跃记忆按 `user_id + scope + fact_type + key` 建立部分唯一索引；upsert 使用 PostgreSQL `ON CONFLICT` 并原子返回 created/updated。
- `last_used_at` 仅在记忆实际参与查询时更新。`agent_run.state_json` 不再复制完整 history、长期记忆或 working memory。

## 用户控制面

- `GET /api/memories` 返回当前认证用户的活跃、未过期、显式、结构化记忆。
- `DELETE /api/memories/{id}` 使用带完整可管理资格谓词的单条 `UPDATE ... RETURNING id` 原子软禁用；不存在、越权、已禁用、过期或隐藏记录统一返回 404，并发第二次禁用返回未找到。
- `ChatResponse.memory_changes` 为向后兼容的可选字段，SSE `done.response` 使用相同结构。
- 前端 ContextPanel 展示“已记住偏好”，覆盖 loading、empty、error、pending forget 状态；登出和身份过期立即清空。
- token refresh 使用绑定 session generation 与原 refresh token 的模块级 single-flight；只有同一认证快照的并发 401 共享 rotation，新登录不会复用旧 promise。旧 401 检测到新认证快照后改用新凭据重试，不能清空新会话。
- 会话列表、会话详情和会话删除都使用 workspace/request version guard；登出或新登录后完成的旧请求不能覆盖工作台，也不能因旧 401 清空新身份。
- policy 追问只接受受控的政策引用/追问词；普通短消息（如“你好”“谢谢”）不会仅因长度短而路由到售后。

## 验证与已知限制

- 单元与无数据库集成测试覆盖 history 预算、V1→V2、偏好作用域/否定、ToolRegistry 同步与流式路径、记忆 API 和 response 序列化。
- 本次最终修复验证为 109 passed、25 skipped；PostgreSQL 相关用例因本机数据库不可用而跳过，atomic disable、active SKU/SPU、exclusion SQL 和跨页 usage exclusion 均有无数据库 statement/behavior 覆盖。
- 后续仍需补真实 PostgreSQL 全量回归、多标签页同会话幂等、生产 token 统计和 policy/knowledge 工具统一。
