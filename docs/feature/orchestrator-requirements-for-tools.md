# 主编排对正式 Tool Contract 的接入需求

## 1. 文档对象与目的

本文面向负责 `backend/app/tools/` 的同学，说明当前 Orchestrator Tool-Calling Loop 对正式
业务 Tool Contract、Registry 和 Executor 的接口需求。

主编排已经可以通过临时 Adapter 跑通。当前没有修改或 mock Tool 的业务逻辑；临时层只补了
LLM-facing name、description、public schema 和 runtime field injection。正式 Contract 到位后，
编排侧将删除或收敛该临时定义。

相关文档：

- Tool 当前能力说明：`docs/feature/tooluse-tools-for-orchestrator.md`
- 主编排当前流程：`docs/feature/orchestrator-tool-loop.md`
- 临时适配代码：`backend/app/agent/tooling.py`

## 2. 双方职责边界

### Tool 模块负责

- Tool Contract 的权威定义和注册。
- LLM-safe name 与 description。
- public/internal input model。
- output model。
- handler 绑定。
- runtime field 声明与安全转换。
- 单次 Tool 的输入校验、timeout、异常归一化和结构化结果。
- `read_only`、`parallel_safe`、`requires_auth` 等执行语义。
- Tool 内部 Repository、Service、Query Planner 和检索实现。

### 主编排负责

- 何时调用哪个 Tool。
- 一个 wave 中生成几个 Tool Calls。
- Tool wave 的顺序与循环上限。
- 将 Tool Result 作为 Observation 返回 Orchestrator。
- clarification、handoff、out-of-scope 和最终自然语言回答。
- LangGraph、SSE、AgentRun、消息持久化和前端事件。

Tool 模块不需要修改 `backend/app/agent/graph.py`，也不需要实现 LangGraph 节点或最终回复。

## 3. 推荐代码归属与目录

正式 Contract 的权威定义建议放在 Tool 模块：

```text
backend/app/tools/
├── contracts.py      # ToolContract、ToolRuntimeContext、handler 类型
├── registry.py       # Contract 注册、查询和单次执行入口
├── schemas.py        # public/internal/output Pydantic models
├── catalog.py        # catalog handlers
├── orders.py         # order handlers
└── knowledge.py      # policy/knowledge handlers
```

如果不希望继续扩大 `schemas.py`，也可以按领域拆分：

```text
backend/app/tools/schemas/
├── catalog.py
├── orders.py
└── knowledge.py
```

要求只有一个：正式 Contract 必须由 `app/tools/` 拥有，不能把
`backend/app/agent/tooling.py` 中的临时 `ToolContract` 当成最终权威类型。

正式代码合并后，`backend/app/agent/tooling.py` 应只保留很薄的 Orchestrator Adapter，或者在
接口完全一致时直接删除。

## 4. 正式 Tool Contract 必需字段

建议的数据结构如下，具体使用 dataclass、Pydantic model 或普通 class 可由 Tool 模块决定：

```python
class ToolContract:
    llm_name: str
    description: str

    public_input_model: type[BaseModel]
    internal_input_model: type[BaseModel]
    output_model: type[BaseModel]

    handler: ToolHandler
    runtime_fields: tuple[str, ...]

    read_only: bool
    parallel_safe: bool
    requires_auth: bool
    timeout_seconds: float | None
```

字段语义：

| 字段 | 要求 |
| --- | --- |
| `llm_name` | 唯一、稳定、可被 OpenAI-compatible Tool Calling 接受 |
| `description` | 明确何时使用、返回什么事实、不能做什么 |
| `public_input_model` | 只包含允许 LLM 提供的参数，建议 `extra="forbid"` |
| `internal_input_model` | handler 的完整可信输入，包含 runtime injected fields |
| `output_model` | Tool Result 的结构化权威 schema |
| `handler` | async 单次执行入口，不负责最终自然语言回答 |
| `runtime_fields` | 由认证/运行环境提供，禁止 LLM 覆盖 |
| `read_only` | 当前 5 个 Tool 应全部为 `True` |
| `parallel_safe` | 只有在 handler、依赖和 Session 生命周期都安全时才能为 `True` |
| `requires_auth` | 至少 `order_lookup` 为 `True` |
| `timeout_seconds` | 单次执行预算；超时返回稳定 error result |

如果需要内部稳定标识，可以额外提供 `internal_name` 或 `registry_name`，但 Orchestrator 只依赖
`llm_name`。

## 5. LLM-safe name

请优先保持当前已联调的名称：

```text
catalog_search
catalog_compare
order_lookup
policy_search
knowledge_search
```

要求：

- 只使用字母、数字和下划线。
- 名称全局唯一。
- 名称是稳定 API；修改会影响 Prompt、Tool transcript、日志 tag 和测试。
- 内部可以继续使用 `catalog.search` 等 dotted name，通过 Registry/Adapter 映射。

正式注册表需要保证：

```python
len({contract.llm_name for contract in contracts}) == len(contracts)
```

## 6. Description 格式

Description 会直接提供给 Orchestrator LLM，应包含：

1. 适用场景。
2. 权威数据范围。
3. 主要返回类型。
4. 关键限制或与相邻 Tool 的区别。

示例：

```text
Search the current PC peripheral catalog for products matching category, brand,
budget, connection type, and specification constraints. Returns structured product,
price, stock, and specification facts. Use catalog_compare instead when the user asks
to compare known products. Do not use this tool for policies or order data.
```

避免只写：

```text
Search products.
```

Description 不应包含 Prompt 注入式指令，也不应让 Tool 声称能够执行其实际不支持的写操作。

## 7. Public Input 与 Internal Input

两者必须分离。

### Public Input

只包含模型可以决定的业务参数：

```python
class OrderLookupPublicInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: int | None = None
    limit: int = Field(default=5, ge=1, le=20)
```

### Internal Input

包含认证和运行环境注入后的完整参数：

```python
class OrderLookupInternalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    order_id: int | None = None
    limit: int = Field(default=5, ge=1, le=20)
```

执行转换：

```text
LLM arguments
    -> validate PublicInput
    -> inject RuntimeContext.user_id
    -> validate InternalInput
    -> handler
```

安全要求：

- `user_id` 不能出现在 `order_lookup` 的 public JSON schema。
- 即使 LLM arguments 手工携带 `user_id`，也必须拒绝，而不是覆盖 Runtime 值。
- 指定订单时 handler 仍必须使用 `user_id + order_id` 联合约束。
- 不允许只按 `order_id` 跨用户查询。

## 8. Runtime Context

当前主编排能可靠提供：

```python
class ToolRuntimeContext:
    user_id: int
```

后续可能增加：

```text
conversation_id
run_id
locale
trace_id
```

请不要提前依赖尚未提供的字段。数据库 Session 当前由 Registry/Service 构造过程管理，不需要
暴露给 LLM。

Contract 应显式声明自己需要的 runtime fields，Executor 在调用前验证字段完整性。

## 9. Handler 签名

推荐 handler 只接收经过验证的 internal input：

```python
ToolHandler = Callable[[BaseModel], Awaitable[BaseModel]]
```

或者使用泛型表达：

```python
async def handler(request: InternalInputT) -> OutputT:
    ...
```

Handler 负责一个业务 action，不负责：

- 选择下一个 Tool。
- 读取 LangGraph state。
- 生成最终客服自然语言。
- 发送 SSE。
- 修改 Orchestrator counter。

## 10. Output Model

每个 Tool 必须声明并返回对应 output model：

| Tool | Output model |
| --- | --- |
| `catalog_search` | `CatalogSearchOutput` |
| `catalog_compare` | `CatalogCompareOutput` |
| `order_lookup` | `OrderLookupOutput` |
| `policy_search` | `DocumentSearchOutput` |
| `knowledge_search` | `DocumentSearchOutput` |

要求：

- 可以通过 `model_dump(mode="json")` 完整序列化。
- 空结果使用明确的 `result_type`，不要用异常表示正常的“查不到”。
- 输出字段保持结构化，不把最终客服回答塞进字符串字段。
- Decimal、datetime 等必须能稳定 JSON 化。
- 调试字段可以保留，但要区分是否适合直接提供给 Orchestrator。

## 11. 统一执行结果与错误

Orchestrator 当前期望稳定的 envelope：

```python
class ToolError(BaseModel):
    code: str
    message: str


class ToolExecutionResult(BaseModel):
    tool_name: str
    ok: bool
    output: dict | None = None
    error: ToolError | None = None
```

成功：

```json
{
  "tool_name": "catalog_search",
  "ok": true,
  "output": {
    "result_type": "products",
    "products": []
  },
  "error": null
}
```

失败：

```json
{
  "tool_name": "catalog_search",
  "ok": false,
  "output": null,
  "error": {
    "code": "invalid_input",
    "message": "..."
  }
}
```

建议稳定支持的 error code：

```text
unknown_tool
invalid_input
unauthorized
forbidden
timeout
dependency_unavailable
execution_error
```

Handler 异常不能直接穿透到 Graph。Registry/Executor 应捕获异常并返回结构化失败结果；日志中
可以保留详细异常，提供给 Orchestrator 的 message 不应包含密钥、连接串或内部堆栈。

## 12. Registry / Provider 接口

编排至少需要：

```python
class ToolContractProvider(Protocol):
    def list_contracts(self) -> Sequence[ToolContract]: ...

    def get_contract(self, llm_name: str) -> ToolContract | None: ...
```

用途：

- `list_contracts()`：为 Router 构造全部 LLM Tool schemas。
- `get_contract()`：Executor 根据原生 Tool Call name 解析正式 Contract。

如正式 Registry 同时负责执行，可以提供：

```python
async def execute(
    llm_name: str,
    public_arguments: dict,
    runtime_context: ToolRuntimeContext,
) -> ToolExecutionResult:
    ...
```

编排侧可以为该 Registry 写一个薄 Adapter，不要求 Tool 模块反向依赖 `app.agent`。

## 13. LLM Tool Schema 导出

每个 Contract 必须能够转换成 OpenAI-compatible schema：

```python
{
    "type": "function",
    "function": {
        "name": contract.llm_name,
        "description": contract.description,
        "parameters": contract.public_input_model.model_json_schema(),
    },
}
```

只允许导出 public input model。Internal input 和 runtime fields 不能出现在 parameters 中。

## 14. Read-only、认证和并发语义

当前 5 个 Tool 都应是 read-only。

建议初始元数据：

| Tool | read_only | requires_auth | parallel_safe 初始建议 |
| --- | --- | --- | --- |
| `catalog_search` | `True` | `False` | `False` |
| `catalog_compare` | `True` | `False` | `False` |
| `order_lookup` | `True` | `True` | `False` |
| `policy_search` | `True` | `False` | 按 embedding/provider 实现确认 |
| `knowledge_search` | `True` | `False` | 按 embedding/provider 实现确认 |

`parallel_safe=True` 不只表示业务上是只读，还必须确认：

- 不共享不可并发的 SQLAlchemy `AsyncSession`。
- 不修改共享可变对象。
- 本地 embedding/model cache 支持并发调用。
- timeout/cancellation 不会破坏其他同 wave 调用。

在这些条件被证明之前，主编排会串行执行。

## 15. Timeout 和取消

每个 Contract 请给出单次 Tool timeout。Executor 应：

- 在 timeout 后取消当前 handler。
- 返回 `ok=false`、`error.code=timeout`。
- 不 commit 部分写入；当前 Tool 均为 read-only。
- 尊重上层任务取消，客户端断开时尽快停止。

主编排不会自动无限重试。Tool Result 会返回 Orchestrator，由它决定追问、换 Tool、解释失败
或转人工。

## 16. 当前 5 个 Contract 的正式交付清单

### `catalog_search`

- Public input：`CatalogSearchInput`
- Internal input：如果没有 runtime field，可以与 public model 相同
- Output：`CatalogSearchOutput`
- Handler：`CatalogToolService.search`
- 限制：只返回商品事实，不回答政策和订单

### `catalog_compare`

- Public input：`CatalogCompareInput`
- Internal input：如果没有 runtime field，可以与 public model 相同
- Output：`CatalogCompareOutput`
- Handler：`CatalogToolService.compare`
- 限制：只返回比较事实，不做最终购买承诺

### `order_lookup`

- Public input：不包含 `user_id`
- Internal input：必须包含 Runtime 注入的 `user_id`
- Output：`OrderLookupOutput`
- Handler：`OrderToolService.lookup`
- `requires_auth=True`
- 必须保持用户隔离

### `policy_search`

- Public input：`DocumentSearchInput`
- Output：`DocumentSearchOutput`
- Handler：`KnowledgeRetrievalToolService.search_policy`
- 文档类型范围由 handler 强制限制，不能完全信任 LLM 参数

### `knowledge_search`

- Public input：`DocumentSearchInput`
- Output：`DocumentSearchOutput`
- Handler：`KnowledgeRetrievalToolService.search_knowledge`
- 当前价格、库存和订单事实不应由该 Tool 返回

## 17. 测试与验收标准

正式 Contract 合并前，请至少覆盖：

### Contract conformance

- 5 个 `llm_name` 唯一且符合命名规则。
- 每个 description 非空且能区分相邻 Tool。
- 每个 public model 能生成 JSON schema。
- public schema 不包含任何 runtime-only field。
- handler 输入和 output model 与 Contract 声明一致。

### Validation and security

- unknown tool 返回 `unknown_tool`。
- 缺少必填字段返回 `invalid_input`。
- LLM 向 `order_lookup` 传 `user_id` 会被拒绝。
- Runtime `user_id` 能正确注入 internal input。
- 用户 B 无法读取用户 A 订单。

### Execution

- 每个 Tool 的正常、空结果和异常路径均返回 `ToolExecutionResult`。
- output 可 JSON 序列化并通过 output model 校验。
- timeout 转换成稳定 error result。
- 测试不依赖真实 LLM API key。

### Orchestrator integration

- `list_contracts()` 可以一次返回全部 5 个 Tool。
- `get_contract(llm_name)` 与 LLM Tool Call name 一致。
- 编排可以不修改 Graph 就替换临时 Provider/Executor。
- Tool Result 可以作为 `ToolMessage` 回传 Router。

## 18. 合并与替换步骤

建议合并顺序：

1. Tool 分支新增正式 `contracts.py` 和完整 Contract models。
2. Registry 注册 5 个正式 Contract，并保留现有 handler 业务逻辑。
3. Tool 测试独立通过。
4. 编排侧新增正式 Registry Adapter，或直接让 Registry 满足 Provider/Executor Protocol。
5. 将 `AgentRuntime` 默认实现切换到正式 Provider/Executor。
6. 运行 Tool、Orchestrator、API、SSE 和用户隔离集成测试。
7. 删除 `StaticToolContractProvider`、临时 description 和临时 `OrderLookupPublicInput`。

预期只有 Adapter/依赖注入层需要调整，不应改写 Graph 节点、Prompt、Tool Loop 或前端协议。

## 19. 需要 Tool 同学确认的问题

正式交付时请明确回复：

1. 正式 Contract 文件和导入路径是什么？
2. 5 个 LLM-safe name 是否保持当前名称？
3. 每个 Tool 的 public/internal/output model 分别是什么？
4. runtime fields 分别有哪些？
5. 哪些 Tool `requires_auth`？
6. 哪些 Tool 经验证可以 `parallel_safe=True`，依据是什么？
7. 每个 Tool 的 timeout 是多少？
8. Registry 是否负责 handler 执行和 error normalization？
9. Tool Result 中哪些字段可以安全提供给 Orchestrator？
10. 是否有需要主编排特殊处理的稳定 error code？
