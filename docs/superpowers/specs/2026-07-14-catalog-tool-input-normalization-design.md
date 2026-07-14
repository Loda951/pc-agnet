# Catalog Tool 输入归一化与流式降级设计

## 背景

`catalog_search` 的公开 schema 要求连接方式位于 `filters.connection_type`。实际模型可能生成语义正确但形状不兼容的顶层 `connection_type`。当前 `AgentRuntime._prepare_tool_call()` 在工具异常边界之外直接执行 Pydantic 校验，因此该输入触发 `ValidationError` 后，SSE 虽已返回 HTTP 200，却只发送 `error` 而没有 `done`，前端最终展示“AI 回复失败”。

## 目标

- 接受模型常见的顶层 `connection_type`，归一化为 `filters.connection_type` 后执行 `catalog.search`。
- 其他非法工具参数不得终止整条 SSE；应记录结构化 `invalid_input` tool result，并由现有编排生成安全降级回答。
- 保持正式 `CatalogSearchInput` schema、ToolRegistry name 映射和 HTTP/SSE 对外契约不变。

## 方案

在 Agent 编排边界增加一个窄范围的 catalog 输入归一化步骤：

1. 复制 LLM 工具参数，不原地修改 decision。
2. 如果存在顶层 `connection_type`，将其迁移到 `filters.connection_type`。
3. 如果 `filters.connection_type` 已由模型显式提供，以嵌套值为准。
4. 再使用 `CatalogSearchInput` 进行严格校验，并继续执行现有的 working memory 与长期偏好合并。

同时将 `_prepare_tool_call()` 放入 `_execute_tool_wave()` 的防御性异常边界。预处理阶段的 `ValidationError` 转换为 `ToolExecutionResult(error.code="invalid_input")`，写入 tool call、tool result 和 SSE `tool_call:error`，随后继续现有的下一轮编排，而不是抛到顶层 SSE `error`。

不放宽 Pydantic 的 `extra="forbid"`，也不允许任意未知字段。归一化仅覆盖已经确认的 `connection_type` 兼容形状。

## 验证

- 回归测试首先复现顶层 `connection_type` 导致运行失败，再验证它被迁移到 `filters` 并成功调用 Registry。
- 增加非法未知字段测试，验证产生 `invalid_input` tool error、最终 `done`，且不会产生顶层 SSE `error`。
- 运行 Agent/tool 聚焦测试、后端全量测试、Ruff 和前端构建。
- 用本地 demo 账号重新请求 `/api/chat/stream`，验证事件以 `done` 结束。
