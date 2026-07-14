# Catalog Tool 输入归一化与流式降级 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `catalog_search` 接受模型生成的顶层 `connection_type`，并确保其他工具参数校验错误不会终止 SSE。

**Architecture:** 在 Agent tool-call 编排边界先执行窄范围参数归一化，再交给严格 Pydantic schema；预处理校验失败转换为现有 `ToolExecutionResult.invalid_input`，由 Orchestrator 继续生成降级回答。正式 ToolContract、Registry 和 schema 均保持不变。

**Tech Stack:** Python 3.11+、Pydantic v2、LangGraph、pytest、FastAPI SSE。

## Global Constraints

- 保持 `CatalogSearchInput.model_config.extra="forbid"`。
- 仅兼容已确认的顶层 `connection_type`，不吞掉其他未知字段。
- 嵌套 `filters.connection_type` 与顶层值冲突时，以嵌套值为准。
- 工具参数错误使用 `invalid_input` tool result，不产生顶层 SSE `error`。

---

### Task 1: Catalog 参数归一化

**Files:**
- Modify: `backend/app/agent/graph.py`
- Test: `backend/tests/test_agent_tool_wiring.py`

**Interfaces:**
- Consumes: `PlannedToolCall.arguments: dict[str, Any]`
- Produces: `_normalize_catalog_search_arguments(arguments: dict[str, Any]) -> dict[str, Any]`

- [x] **Step 1: 写失败测试**

新增测试，构造包含顶层 `connection_type="Wireless"` 的 `catalog_search` call，调用 `AgentRuntime._prepare_tool_call()`，断言返回参数不再包含顶层字段，且 `filters.connection_type == "Wireless"`。再增加冲突用例，断言已有嵌套值 `Wired` 优先。

- [x] **Step 2: 验证测试因当前 ValidationError 失败**

Run: `cd backend && .venv/bin/pytest -q tests/test_agent_tool_wiring.py -k "top_level_connection_type or nested_connection_type"`

Expected: FAIL，错误包含 `CatalogSearchInput` 和 `connection_type Extra inputs are not permitted`。

- [x] **Step 3: 写最小实现**

在 `graph.py` 增加：

```python
def _normalize_catalog_search_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(arguments)
    connection_type = normalized.pop("connection_type", None)
    filters = normalized.get("filters")
    if connection_type is not None and (filters is None or isinstance(filters, dict)):
        normalized_filters = dict(filters or {})
        normalized_filters.setdefault("connection_type", connection_type)
        normalized["filters"] = normalized_filters
    return normalized
```

在 `_prepare_tool_call()` 中先调用该函数，再执行 `CatalogSearchInput.model_validate()`。

- [x] **Step 4: 验证归一化测试通过**

Run: `cd backend && .venv/bin/pytest -q tests/test_agent_tool_wiring.py -k "top_level_connection_type or nested_connection_type"`

Expected: 2 passed。

### Task 2: 预处理校验错误降级

**Files:**
- Modify: `backend/app/agent/graph.py`
- Test: `backend/tests/test_agent_tool_wiring.py`

**Interfaces:**
- Consumes: `AgentRuntime._prepare_tool_call()` 的 `ValidationError`
- Produces: `ToolExecutionResult(error.code="invalid_input")` 和正常完成的 tool wave

- [x] **Step 1: 写失败测试**

构造包含未知字段 `unexpected_filter` 的 `catalog_search` decision，通过仅包含 `execute_tool_wave` 的 LangGraph 运行节点。断言节点不抛异常，`tool_results[0].execution.ok` 为 false，错误码为 `invalid_input`，且 executor/registry 未被调用。

- [x] **Step 2: 验证测试因 ValidationError 逃逸失败**

Run: `cd backend && .venv/bin/pytest -q tests/test_agent_tool_wiring.py -k "invalid_catalog_tool_arguments"`

Expected: FAIL，`ValidationError` 从 `_prepare_tool_call()` 逃逸。

- [x] **Step 3: 写最小实现**

在 `_execute_tool_wave()` 每个 call 内先保留 `planned_call`，捕获 `_prepare_tool_call()` 的 `ValidationError` 并生成：

```python
ToolExecutionResult(
    tool_name=planned_call.name,
    ok=False,
    error=ToolError(code="invalid_input", message=str(exc)),
)
```

保持 started/error SSE custom event、tool call 审计、tool wave/result 记录和 `_apply_tool_output()` 行为不变。

- [x] **Step 4: 验证聚焦测试通过**

Run: `cd backend && .venv/bin/pytest -q tests/test_agent_tool_wiring.py`

Expected: 全部通过，数据库测试可按既有环境条件 skip。

### Task 3: 真实复现与全量验证

**Files:**
- Modify: `docs/superpowers/plans/2026-07-14-catalog-tool-input-normalization.md`（勾选执行状态）

**Interfaces:**
- Consumes: 本地 demo 账号与 `/api/chat/stream`
- Produces: 以 SSE `done` 结束的真实请求证据

- [x] **Step 1: 运行后端全量验证**

Run: `cd backend && .venv/bin/pytest -q && .venv/bin/ruff check .`

Expected: 0 failed，Ruff `All checks passed!`。

- [x] **Step 2: 运行前端构建**

Run: `cd frontend && npm run build`

Expected: TypeScript 与 Vite 构建成功。

- [x] **Step 3: 重放真实 SSE 请求**

使用 demo 账号请求“推荐 500 元以内无线鼠标”，记录 HTTP status 和 event type，不输出 token。

Expected: HTTP 200；事件包含 `tool_call`、`context`、`done`，不包含顶层 `error`。

- [x] **Step 4: 提交修复**

```bash
git add backend/app/agent/graph.py backend/tests/test_agent_tool_wiring.py
git commit -m "fix: normalize catalog tool arguments"
```
