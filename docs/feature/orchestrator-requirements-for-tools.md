# 主编排接入 Tool 后的剩余收口事项

## 1. 文档状态与目的

正式 Tool Contract 已完成并接入主编排。本文件不再重复已经交付的字段、模型和接入要求，
只记录 Tool 模块后续可以继续收口的事项，供 Tool 与 Orchestrator 负责人协作。

当前结论：正式 Contract 已经可以驱动现有主流程，但以下三项属于正式 Tool 接口仍需完成的
收口要求。它们分别保证 Tool 注册不会漂移、Orchestrator 能稳定处理失败，以及 LLM 生成的
未知参数不会被静默忽略。

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

- 5 个稳定的 LLM-safe name：`catalog_search`、`catalog_compare`、`order_lookup`、
  `policy_search`、`knowledge_search`。
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

### 3.1 保证 Contract 与 Registry 使用同一注册事实

现状：

- `contracts.py` 定义 LLM metadata、输入输出模型和执行语义。
- `registry.py` 另外注册 dotted registry name、input model 和 handler。

当前通过 `registry_name` 正常衔接，但两处信息未来可能发生漂移。这会让 LLM 看到的 Tool、
Executor 校验的 schema 和 Registry 实际执行的 handler 对不上，因此需要正式收口。

优先建议让一个权威注册对象同时持有：

- `ToolContract`
- handler
- internal registry name（如果仍需要）

并在构建 Registry 时从同一组定义生成，而不是分别维护两张注册表。

如果当前不希望调整 Registry 结构，最低验收方案是增加自动一致性测试，逐项验证：

- 每个 Contract 的 `registry_name` 都已注册。
- Registry 不存在未被正式 Contract 覆盖的业务 Tool。
- Contract 的 internal input model 与 Registry input model 相同。
- 每个 Contract 声明的 output model 能校验对应 handler 的正常、空结果输出。

验收建议：

- 新增或修改 Tool 时只需要改一个权威定义。
- Contract 的 internal input model 与 Registry handler input 不可能不一致。
- Contract 与 handler 的一一对应关系有测试保障。

### 3.2 统一稳定错误码，供 Orchestrator 处理失败

现状：Registry 捕获 handler 异常时会使用异常类名作为 error code，例如
`RuntimeError`。因为 Registry 已经返回失败结果，外层 `RegistryToolExecutor` 不会再次把它
归一化为稳定错误码。

建议稳定支持：

```text
unknown_tool
invalid_input
unauthorized
forbidden
timeout
dependency_unavailable
execution_error
```

建议：

- handler 的未知异常统一映射为 `execution_error`。
- 数据库、embedding 或本地索引不可用时按实际情况映射为 `dependency_unavailable`。
- 详细异常和堆栈只写服务端日志。
- 返回 Orchestrator 的 `error.message` 使用安全、稳定、可解释的摘要，不包含连接串、路径、
  密钥或内部堆栈。
- Registry 和 Executor 不返回 Python 异常类名作为对外 error code。

Orchestrator 需要能够依赖以下语义：

- `invalid_input`：Tool Call 参数不合规，可重新规划参数或向用户澄清。
- `timeout`：本次查询超时，可返回超时提示；是否重试由主编排预算决定。
- `dependency_unavailable`：数据库、索引或其他 Tool 依赖暂时不可用。
- `unauthorized` / `forbidden`：停止执行，不通过重复 Tool Call 绕过权限。
- `execution_error`：未分类的内部执行失败，使用统一失败说明。

### 3.3 所有 public input model 禁止未知字段

现状：`OrderLookupPublicInput` 已配置 `extra="forbid"`；商品和文档搜索模型仍使用 Pydantic
默认的未知字段忽略行为。

请为所有 LLM-facing public input model 配置 `extra="forbid"`，包括商品搜索、商品对比、
订单查询、政策搜索和知识搜索。模型生成未知字段时应稳定返回 `invalid_input`，不能忽略未知
字段后继续执行。

这主要是正确性要求。例如模型把 `max_price` 错写成 `max_budget` 时，如果 Tool 静默忽略，
Orchestrator 会误以为搜索已经应用预算限制。禁止未知字段后，Orchestrator 可以根据
`invalid_input` 重新规划或澄清。

验收建议：每个 public input model 至少有一个“携带未知字段”的测试，并断言 Registry handler
没有被调用。

## 4. 当前主编排边界

在以上事项完成前，主编排继续遵循以下约束：

- 只接入这 5 个 read-only Tool。
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
3. 5 个 Tool 的 public input schema 是否全部禁止未知字段，以及对应测试结果。
4. 是否有 Tool 经验证可以改为 `parallel_safe=True`，以及验证依据。

这些变化原则上应限制在 Tool Contract、Registry、Executor 和测试层，不需要重写 LangGraph、
Tool Loop、SSE 或前端协议。
