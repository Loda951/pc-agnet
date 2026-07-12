# 主编排接入 Tool 后的剩余收口事项

## 1. 文档状态与目的

正式 Tool Contract 已完成并接入主编排。本文件不再重复已经交付的字段、模型和接入要求，
只记录 Tool 模块后续可以继续收口的事项，供 Tool 与 Orchestrator 负责人协作。

当前结论：正式 Contract 已经可以驱动现有主流程，但以下两项属于正式 Tool 接口仍需完成的
收口要求。它们分别保证 Tool 注册不会漂移，以及 Orchestrator 能稳定区分和处理不同失败。

相关实现：

- 正式 Contract：`backend/app/tools/contracts.py`
- Tool Registry：`backend/app/tools/registry.py`
- Tool schemas：`backend/app/tools/schemas.py`
- 主编排：`backend/app/agent/graph.py`
- Tool 能力说明：`docs/feature/tooluse-tools-for-orchestrator.md`
- 主编排流程：`docs/feature/orchestrator-tool-loop.md`

## 2. 当前已经完成并正式采用的能力

当前主编排直接依赖 `app.tools.contracts`，不再由 `app.agent` 维护临时 Contract 或 Adapter。

已经正式采用：

- 6 个稳定的 LLM-safe name：`catalog_search`、`catalog_compare`、`catalog_facets`、
  `order_lookup`、`policy_search`、`knowledge_search`。
- 每个 Tool 的 description、public input model、internal input model 和 output model。
- OpenAI-compatible Tool schema 导出，并且只导出 public input。
- `order_lookup` 的 `user_id` 由可信 Runtime 注入，不暴露给 LLM。
- `read_only`、`parallel_safe`、`requires_auth` 和 `timeout_seconds` 元数据。
- public/internal input 校验、output model 校验、timeout 处理和结构化执行结果。
- 主编排通过 `DefaultToolContractProvider` 获取全部 Contract，通过
  `RegistryToolExecutor` 调用现有 Tool Registry。
- 当前所有 Tool wave 继续串行执行；所有 Contract 的 `parallel_safe` 都保持 `False`。

因此，原先关于 Contract 字段、LLM-safe name、public/internal input 分离、Runtime 注入、
output model、timeout 和 Graph 替换步骤的待交付要求均已从本文删除。

## 3. 剩余正式要求

### 3.1 将 Contract、Registry 和 Handler 收敛为单一 Tool Definition 事实源

当前实现分别维护两组注册事实：

- `contracts.py` 定义 LLM name、internal registry name、description、public/internal input model、
  output model 和执行 metadata。
- `registry.py` 再次定义 internal registry name、input model 和实际 handler。

现有一致性测试只验证 `contract.registry_name` 集合与 `registry.tool_names` 集合相同。这能防止
名称遗漏，但不能发现以下漂移：

- Contract 和 Registry 使用了不同的 input model。
- Contract 声明的 output model 与 handler 实际返回结构不一致。
- registry name 存在，但绑定到了错误的 handler。
- Tool 的认证、timeout、read-only 或并发 metadata 与实际执行方式不一致。

本要求的目标不是让 `llm_name` 与 `registry_name` 使用相同字符串。两者可以继续分别使用
`catalog_search` 和 `catalog.search`，因为它们属于不同命名空间。真正需要保证的是：这两个名称
之间的映射、输入输出模型、执行 metadata 和 handler 只能在一个权威 Tool Definition 中组合
和注册一次。

#### 推荐结构

保留 `ToolContract` 作为静态、LLM-facing 的接口描述，再增加一个运行时组合对象：

```python
@dataclass(frozen=True)
class BoundTool:
    contract: ToolContract
    handler: ToolHandler
```

由单一 `ToolCatalog` 保存全部 `BoundTool`：

```text
ToolCatalog
  ├── BoundTool
  │    ├── ToolContract
  │    │    ├── llm_name
  │    │    ├── registry_name
  │    │    ├── description
  │    │    ├── public_input_model
  │    │    ├── internal_input_model
  │    │    ├── output_model
  │    │    ├── runtime_fields
  │    │    ├── timeout_seconds
  │    │    └── read_only / parallel_safe / requires_auth
  │    └── handler
  └── ...
```

`ToolCatalog` 应同时按 `llm_name` 和 `registry_name` 建立只读索引，并在构建时验证名称唯一、
Contract 与 handler 一一对应。

主编排需要的两个接口都从同一个 Catalog 派生：

```text
ToolContractProvider
    -> 从 ToolCatalog 导出 Contract 和 LLM schema

ToolExecutor
    -> 从同一个 ToolCatalog 获取 BoundTool、校验并执行 handler
```

这样新增或修改 Tool 时只修改一个权威定义，不再分别修改 `default_tool_contracts()` 和
`build_tool_registry()` 两张注册表。

不建议把 session-bound handler 直接放进当前静态 Pydantic `ToolContract`。Catalog、Order 等
handler 依赖当前 SQLAlchemy Session 和运行时 service，使用 `BoundTool = Contract + Handler`
可以同时保持 Contract 的静态可导出性和 handler 的运行时生命周期。

如果确认 dotted `registry_name` 没有其他消费者，后续可以进一步只使用 `llm_name`；但删除
`registry_name` 不是本次收口的前置条件。

#### 推荐迁移顺序

1. 新增 `BoundTool` 和 `ToolCatalog`，暂时保留现有 Provider、Registry 和 Executor 接口。
2. 新增一个 runtime builder，使用当前 Session、Settings 和 service 构建全部内置 BoundTool。
3. 让 `DefaultToolContractProvider` 从 ToolCatalog 读取 Contract。
4. 让 `RegistryToolExecutor` 从同一个 ToolCatalog 解析并执行 handler。
5. 保留兼容 Adapter 跑完回归测试，再删除重复的 `default_tool_contracts()` / `registry.register()`
   注册路径。

#### 最低验收标准

- 新增或修改一个 Tool 时只需要修改一个权威 Tool Definition。
- `llm_name` 和 `registry_name` 分别唯一，且映射只定义一次。
- 每个 Contract 恰好绑定一个 handler，每个公开业务 handler 恰好对应一个 Contract。
- `contract.internal_input_model` 与 handler 接收的 input model 一致。
- handler 的正常、有业务结果为空、能力不支持等输出都能通过 `contract.output_model` 校验。
- Catalog 构建时发现重复名称、缺失 handler 或模型错配应立即失败，不能等到真实请求执行。
- 自动测试应刻意构造名称、input model、output model 和 handler 错配，证明保护机制有效。

如果当前迭代不实施结构重构，过渡方案至少应补全上述名称、模型、handler 和输出一致性测试；
只比较两个 name 集合不能视为本要求完成。

### 3.2 统一 Tool Result 语义和稳定错误分类，供 Orchestrator 合成回复

主编排会把完整 Tool Result 作为 Observation 交给 Orchestrator LLM。不同结果必须支持不同的
回复角度，因此 Tool 层首先需要明确区分：

1. **查询成功且有数据**：`ok=true`，由结构化 `output` 返回事实。
2. **查询成功但没有业务结果**：`ok=true`，通过 Tool output 中的 `result_type=empty`、
   `not_found` 等业务结果表达。这不是系统错误。
3. **查询能力不支持该问题**：仍属于成功执行的业务判断，例如商品 Tool 返回
   `unsupported_query`。Orchestrator 应说明当前数据能力不支持，而不是说“系统故障”。
4. **Tool 执行失败**：`ok=false`，通过稳定的 `error` 结构说明失败类别和恢复建议。

上述语义不能混用。例如：

- 商品查询为空时，Orchestrator 可以说明当前没有匹配商品，并询问是否放宽预算或规格。
- 订单 `not_found` 时，可以请用户检查订单号，但不能声称订单服务异常。
- 文档检索为空时，只能说明没有找到足够依据，不能用模型记忆补充商城政策。
- 数据库或知识索引不可用时，应说明查询服务暂时不可用，不能回答成“没有该商品、订单或政策”。

当前 Registry 已经把 handler 未知异常收敛为 `execution_error`，但失败结果仍只有 `code` 和
`message`，且尚未区分数据库、embedding、本地索引等依赖异常。建议稳定支持以下错误码：

```text
unknown_tool
invalid_input
unauthorized
forbidden
timeout
dependency_unavailable
execution_error
```

错误结构建议至少包含：

```text
code
message
retryable
recommended_action
```

其中：

- `code`：供工程代码和 Prompt 使用的稳定机器语义。
- `message`：提供给 Orchestrator LLM 的安全、简短、可解释摘要，用于最终回复合成。
- `retryable`：表示是否值得在当前 Tool Loop 预算内重试，不代表未来永远不能重试。
- `recommended_action`：减少 LLM 仅凭自然语言猜测恢复策略，建议限制为稳定枚举，例如
  `replan_arguments`、`ask_user`、`retry_once`、`explain_temporary_unavailability`、
  `request_authentication`、`stop`、`handoff`。

建议映射：

- handler 的未知异常统一映射为 `execution_error`。
- 数据库、embedding 或本地索引不可用时按实际情况映射为 `dependency_unavailable`。
- 缺少有效认证上下文时使用 `unauthorized`；明确无权访问时使用 `forbidden`。
- 订单归属校验仍可返回 `not_found`，避免通过错误类型泄露其他用户的订单是否存在。
- 详细异常和堆栈只写服务端日志。
- 返回 Orchestrator 的 `error.message` 使用安全、稳定、可解释的摘要，不包含连接串、路径、
  密钥或内部堆栈。
- Registry 和 Executor 不返回 Python 异常类名作为对外 error code。
- 如需排查，可额外返回不含内部信息的 `error_id`，由服务端日志关联真实异常。

示例：

```json
{
  "code": "invalid_input",
  "message": "max_price 必须是非负数，且不能小于 min_price",
  "retryable": true,
  "recommended_action": "replan_arguments"
}
```

```json
{
  "code": "dependency_unavailable",
  "message": "商品目录服务暂时不可用",
  "retryable": false,
  "recommended_action": "explain_temporary_unavailability"
}
```

Orchestrator 需要能够依赖以下恢复语义：

- `invalid_input`：Tool Call 参数不合规，可重新规划参数或向用户澄清。
- `timeout`：本次查询超时，可返回超时提示；是否重试由主编排预算决定。
- `dependency_unavailable`：数据库、索引或其他 Tool 依赖暂时不可用。
- `unauthorized` / `forbidden`：停止执行，不通过重复 Tool Call 绕过权限。
- `execution_error`：未分类的内部执行失败，使用统一失败说明。

验收建议：

- 分别测试“有数据”“业务空结果”“能力不支持”“执行失败”，并断言四者不会相互混淆。
- 每个稳定错误码至少覆盖一次 `retryable` 和 `recommended_action`。
- `invalid_input` 能驱动重新规划或澄清，`dependency_unavailable` 不会被回答为业务无结果。
- `unauthorized` / `forbidden` 不会触发重复 Tool Call 绕过权限。
- 未知异常的返回内容不包含 Python 异常类名、连接信息、本地路径或堆栈。
- 主编排使用 fake Tool Result 验证不同结果能生成不同回复策略，不依赖真实外部服务。

## 4. 当前主编排边界

在以上事项完成前，主编排继续遵循以下约束：

- 只接入这 6 个 read-only Tool。
- 同一个 wave 内串行调用 Tool。
- 订单身份只接受 Runtime 注入值。
- Tool 失败结果返回 Orchestrator，由 Orchestrator 决定解释、追问、换 Tool 或转人工。
- 不把 `parallel_safe` 改为 `True`，除非 Session 生命周期、handler 和依赖都已验证可并发。

当前项目不会在主编排以外复用 `RegistryToolExecutor`，因此不要求额外设计通用 Executor
认证入口。订单认证继续由 Chat API、Runtime `user_id` 注入和订单 Repository 用户隔离共同保证。

当前 Tool 均为团队内部受控实现，因此本阶段不要求新增独立 output projection 层。新增输出
字段时仍不得包含密钥、连接信息、内部堆栈或无权限订单数据。

## 5. 后续交付时请同步的信息

处理上述事项时，请在 PR 或 Tool 文档中说明：

1. Contract 与 handler 是否已经统一为单一注册源；如果没有，使用了哪些一致性测试保证对齐。
2. 最终稳定错误码及每类依赖异常的映射规则。
3. 是否有 Tool 经验证可以改为 `parallel_safe=True`，以及验证依据。

这些变化原则上应限制在 Tool Contract、Registry、Executor 和测试层，不需要重写 LangGraph、
Tool Loop、SSE 或前端协议。
