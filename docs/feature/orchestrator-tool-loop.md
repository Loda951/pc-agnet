# 主编排进度：受限 Orchestrator Tool-Calling Loop

## 1. 文档目的

本文记录当前主编排的实际实现，用于后续迭代、评审和联调。它描述的是
`backend/app/agent/` 中已经可以运行的 Graph，不是远期设想。

本阶段的核心变化是：主流程不再先做独立 intent 分类再进入固定检索链，而是由一个受限的
Orchestrator LLM 直接选择终态或业务 Tool Call。Tool Call 本身就是本轮路由结果。

当前只改写编排层。商品、订单、知识检索、记忆、Repository、数据库模型和业务 Tool 内部
实现均保持独立。

## 2. 当前架构原则

- Orchestrator 每次看到全部 6 个业务 Tool，不做渐进式披露。
- 业务事实必须来自 Tool Result，不能由 LLM 编造。
- `intent` 不参与控制流，只作为兼容的日志和前端 tag 字段。
- 同一个 AIMessage 中的多个 Tool Calls 属于同一个 action wave。
- 每个业务 Tool Call 带一个仅供编排使用的 `subquery` 元数据；Runtime 在调用业务 Tool 前剥离，
  不改变正式 Tool input contract。
- Orchestrator 只使用 Tool 的 public input schema。`catalog_search` 与 `catalog_facets` 采用
  query-first 输入，商品类目、价格、规格、facet 等结构化查询计划只由 Tool 内部 Planner 生成。
- 有依赖关系的 Tool Call 必须等前一轮 Observation 返回后，在下一次 Orchestrator 调用中生成。
- 最多执行 2 个 Tool wave，最多调用 3 次 Orchestrator LLM。
- `handoff` 和 `out_of_scope` 使用确定性模板，不采用模型自由生成的正文。
- Tool 参数中的认证字段由可信 Runtime 注入，不能由模型提供。
- `POST /api/chat` 是前端默认入口，通过 `AgentRuntime.run()` 一次性返回完整响应。
- SSE 接口暂时保留为兼容入口；两种接口运行同一份 LangGraph，不改变 Orchestrator 决策。

## 3. 总体流程图

```text
HTTP Request
  -> Pydantic validation + authenticated user
  -> load_context
  -> orchestrate
  -> dispatch_decision

dispatch_decision
  |-- tool_calls
  |     -> execute_tool_wave
  |     -> normalize_tool_results
  |     -> update_subquery_ledger
  |     -> Tool Results / Observations
  |     -> orchestrate                    # 进入下一轮决策
  |
  |-- control action
  |     -> terminal_guard
  |     -> finalize_response / render_*_template
  |     -> persist_turn
  |     -> END
  |
  |-- handoff
  |     -> render_handoff_template
  |     -> persist_turn
  |     -> END
  |
  `-- out_of_scope
        -> render_out_of_scope_template
        -> persist_turn
        -> END
```

其中只有 `execute_tool_wave -> Tool Results / Observations -> orchestrate` 会回到循环；
`finalize_response`、`render_handoff_template` 和 `render_out_of_scope_template` 都是进入
`persist_turn` 的终态路径。

概念上可以将它分成四层：

```text
Context Assembly
    load_context

Cognitive / Policy Layer
    orchestrate

Deterministic Control and Action Layer
    dispatch_decision
    execute_tool_wave
    finalize_response
    render_*_template

Durability and Transport Layer
    persist_turn
    complete ChatResponse
    legacy SSE custom events
```

## 4. 决策模型

Orchestrator 每次只能产生以下一种决策：

| Decision | 是否调用 Tool | 用户可见内容 | 后续节点 |
| --- | --- | --- | --- |
| `direct_response` | 否 | `finish_direct.response` | `terminal_guard` |
| `clarification` | 否 | `ask_clarification.response` | `terminal_guard` |
| `grounded_response` | 否 | `finish_answer.response` | `terminal_guard` |
| `partial_response` | 否 | `finish_partial.response` | `terminal_guard` |
| `unavailable_response` | 否 | 安全不可用说明 | `terminal_guard` |
| `handoff` | 否 | 固定模板 | `render_handoff_template` |
| `out_of_scope` | 否 | 固定模板 | `render_out_of_scope_template` |
| `tool_calls` | 是 | 当前不输出正文 | `execute_tool_wave` |

典型判断逻辑：

```text
身份、能力、使用方式等无需业务事实
    -> direct_response

缺少必要条件，尚不能安全选择 Tool 或回答
    -> clarification

商品、订单、物流、政策、FAQ、品牌或外设知识事实
    -> tool_calls

Observation 已充分支持回答
    -> grounded_response

退款办理、退换货办理、维修、订单修改、代下单等写操作
    -> handoff

明显超出 PC 外设商城客服范围
    -> out_of_scope
```

## 5. 节点说明

### 5.1 `load_context`

类型：确定性应用节点，包含 Repository I/O，不调用 LLM。

职责：

- 获取或创建当前用户的 conversation。
- 读取最近 6 条 user/assistant 会话历史。
- 保存当前 user message。
- 创建本轮 `AgentRun`。
- 初始化 `conversation_id`、`user_message_id`、`run_id` 和 history。

该节点只组装 session context，不做意图判断，不选择 Tool，也不读取或改写 Tool 内部状态。

### 5.2 `orchestrate`

类型：LLM policy/planning 节点；无 LLM key 时存在 rule-based 开发降级路径。

生产主路径：

- 使用绑定了全部 Tool Contract 的 Chat Model。
- 输入 System Prompt、最近会话历史、当前用户输入和已完成的 Tool Observations。
- 选择一个终态或生成一批原生 Tool Calls。
- 校验 Tool 名称、执行预算和终态协议。
- 将本轮 Tool 名称写入兼容字段 `intent`，仅用于日志和前端 tag。

开发降级路径：

- 仅在没有配置 LLM 时使用现有 rule-based 分类函数生成兼容决策。
- 用于本地开发和确定性测试，不是生产主路由。

需要特别区分：商品 Tool 内部可能自行使用 `LLMCatalogQueryPlanner` 或 rule-based planner，
那属于 Tool 的内部实现，不是主 Orchestrator 节点的一部分。

Orchestrator Prompt 只维护跨 Tool 的选择和事实来源规则，不复制 Contract 中的参数 schema：

- `catalog_search`、`catalog_compare`、`catalog_facets`、`order_lookup` 属于结构化业务事实工具，
  分别负责当前商品、目录聚合、订单和物流事实。
- `policy_search`、`knowledge_search` 属于文档证据工具，检索结果只能支持文档明确覆盖的政策、
  FAQ、品牌和选购知识，不能替代价格、库存、SKU、订单或物流事实。
- Prompt 使用少量对比案例约束高混淆边界，例如“有哪些鼠标品牌”使用 `catalog_facets`，
  “某品牌是什么”使用 `knowledge_search`，“某品牌有哪些鼠标”使用 `catalog_search`。
- 结构化查询为空和文档检索为空具有不同语义；Tool 执行失败也不能等同于空结果。
- 单个 Tool 的详细用途、输入和输出继续由正式 Contract description 与 JSON schema 提供。
- query-first Tool 的首次 `query` 可以把用户表达整理成语义等价、自包含的自然语言，并补全当前
  对话中已确认的必要上下文；不得增加用户未表达的条件。首次执行后，该 subquery 的
  `canonical_query` 在当前 turn 内冻结。Orchestrator 不生成或覆盖 Tool 内部的 `category`、
  `filters`、`preference_defaults` 等领域规划字段。

Prompt 采用“稳定策略 + 动态运行时上下文 + Tool Observation”的分层方式，代码位于：

```text
backend/app/agent/prompts/
├── static.py    # 稳定身份、目标、边界、事实源、记忆、Tool loop、终态与表达风格
├── dynamic.py   # 当前请求 envelope、execution_state、memory_context、按需失败恢复策略
└── __init__.py  # 对 graph 暴露公共 builder 和兼容常量
```

没有拆成独立 `soul.md`、`agent.md` 和 `user.md` 文件：当前系统只有一个业务 Orchestrator
persona，使用代码内具名分块可以避免运行时文件读取和 Python package data 打包风险，同时保持
每个分块可单测。System Prompt 使用 XML 风格标签区分：

- `agent_identity` 与 `primary_objective`：稳定角色和成功标准。
- `runtime_model` 与 `decision_policy`：真实 LangGraph 状态机、调用预算、native Tool Call
  契约和何时直接回答/澄清/调用工具。
- `instruction_priority`、`scope_and_safety`：优先级、只读边界和 prompt injection 防护。
- `fact_sources`、`memory_policy`、`tool_routing`：业务事实、记忆和工具职责。
- `tool_loop_policy`：成功、空结果、失败、部分成功和停止条件。
- `terminal_response_contract` 与 `response_style`：完整正文协议和用户可见表达。

当前用户请求、execution state 和 memory context 由 `build_orchestrator_user_prompt()` 放在独立
标签中。原始 Tool Result 仍只通过 `ToolMessage` 传递，不复制进普通用户正文。

当且仅当本轮存在 `execution.ok=false` 时，`build_orchestrator_system_prompt()` 才追加
`tool_failure_recovery`：

| recommended_action | 动态规则 |
| --- | --- |
| `retry_once` | 相同 Tool 与参数最多重试一次；相同错误失败两次后停止 |
| `replan_arguments` | 根据 `invalid_input` 修正参数，不得原样重交 |
| `explain_temporary_unavailability` | 不重复调用同一依赖，说明暂时不可用 |
| `request_authentication` | 停止查询并要求恢复认证 |
| `stop` | 不重试，使用成功的其他结果或安全结束 |

动态分块只提升运行时归一化后的 Tool 名称、错误码、动作和尝试次数，不提升原始 error message，
避免把 Tool 输出中的非可信文本放进高优先级指令。`ok=true/result_type=empty` 不加载失败分块，
仍按“查询成功但无匹配结果”结束或询问是否放宽条件。动态分块中的“存在成功结果”以 normalized
outcome 的 `has_usable_information=true` 为准，不再把 `ok=true` 的空结果算作可用证据。

### 5.3 `dispatch_decision`

类型：确定性 conditional edge，不调用 LLM，不执行 Tool。

职责是把已经校验的 Decision 映射到下一节点：

```text
tool_calls       -> execute_tool_wave
handoff          -> render_handoff_template
out_of_scope     -> render_out_of_scope_template
其他用户终态     -> finalize_response
```

它不重新理解用户输入，也不重新分类 intent。

### 5.4 `execute_tool_wave`

类型：确定性 action execution 节点；由工程代码调用业务 Tool。

职责：

1. 根据 LLM-safe name 查找 Tool Contract。
2. 使用 public input model 校验 LLM arguments。
3. 规范化为 public schema 允许的字段，禁止把 Tool 内部规划字段重新注入调用。
4. 从可信 Runtime 注入 `user_id` 等字段。
5. 调用 `ToolExecutor`。
6. 将 handler 异常归一化为结构化 `ToolExecutionResult`。
7. 保存 Tool Call 输入和输出。
8. 把结构化输出投影到 `products`、`evidence`、`order` 等兼容状态。
9. 将完整 Tool Result 作为下一次 Orchestrator 调用的 Observation。

Catalog Tool 在 `ok=true` 时还可能返回 `diagnostics`。Runtime 将 `unsupported_query` 归一化为
`unsupported`，将无有效商品的 `invalid_catalog_plan` 归一化为 `insufficient`，避免把内部 Planner
失败误判为真实空结果；`empty_result` 才表示有效查询没有匹配数据。

当前一个 wave 内串行执行 Tool。原因是数据库 Tool 共享 SQLAlchemy `AsyncSession`，在正式
Contract 明确 `parallel_safe` 且 Session 生命周期支持并发之前，不假设可以并行。

### 5.5 `finalize_response`

类型：确定性 response assembly 节点，不再次调用 LLM。

Orchestrator 已经返回不含 Tool Call 的完整用户正文。运行时根据是否存在成功 Tool Result，
在内部把它记录为 `direct_response` 或 `grounded_response`。该节点：

- 把完整正文写入 `state.answer`。
- 当模型正文为空时使用确定性 fallback。
- 生成 suggested actions。

主接口在该节点完成并持久化后返回完整 `ChatResponse`。兼容 SSE 接口只发送单个完整正文
`delta`，不发送 token/chunk 增量。

### 5.6 `render_handoff_template`

类型：确定性安全终态节点。

适用于需要人工确认或执行的写操作。它忽略模型可能生成的正文，统一设置：

- `boundary.classification = human_handoff_required`
- 固定 handoff 文案
- 人工接管 suggested action 与当前输入中可确定的订单号/请求类型

### 5.7 `render_out_of_scope_template`

类型：确定性安全终态节点。

适用于明显超出商城客服范围的请求。它忽略模型正文，使用固定 OOS 模板，并给出回到外设
商城服务范围的 suggested action。

### 5.8 `persist_turn`

类型：确定性 durability 节点，包含 Repository/transaction I/O。

职责：

- 保存最终 assistant message。
- 保存 decision、boundary、Tool tag、products、evidence 和 order metadata。
- 完成 `AgentRun` 并保存可 JSON 序列化的最终 state。
- commit transaction。

只有形成合法终态后才进入该节点。

## 6. Tool Loop 与预算控制

状态中显式记录：

```text
orchestrator_call_count
tool_wave_count
tool_waves
tool_results
```

最大链路为：

```text
Orchestrator #1
    -> Tool Wave #1
    -> Orchestrator #2
    -> Tool Wave #2
    -> Orchestrator #3
    -> 用户终态
```

约束：

- `orchestrator_call_count <= 3`
- `tool_wave_count <= 2`
- 第 3 次 Orchestrator 不允许再发起 Tool Call。
- 达到限制仍请求 Tool 时，工程代码按当前 active 结果确定性结束：已有 usable 结果时生成完整或
  部分回答，没有 usable 结果时才生成 unavailable/clarification；不发起第 4 次 LLM，也不丢弃
  前序可用结果。该终止与普通请求共用相同的 answer、partial、unavailable、clarification 语义，
  用户回复不暴露调用次数、wave、预算或处理上限。
- Tool 失败不是自动结束条件，但下一 wave 只允许处理首轮已声明且仍未解决的 subquery、错误协议
  允许的恢复，或明确依赖上一轮结果的查询。
- 同一 subquery 的后续调用必须保持 `canonical_query` 不变：timeout 只能原样重试一次；
  invalid_input 只能修改错误明确指出的非 query 参数；insufficient/invalid_catalog_plan 不允许由
  Orchestrator 改写 query 补救。用户新一轮输入和明确的依赖型新操作可以生成新的 query。
- 推荐结果只要与用户明确条件相关并能回答核心问题，就视为充分；用户没有要求多个品牌、指定
  数量或更多备选时，不为了结果丰富度继续调用 Tool。
- `catalog_search` 至少一个相关 usable 商品、`catalog_compare` 至少两款 usable 商品、
  `policy_search` / `knowledge_search` 至少一篇能直接支持核心问题的 usable 文档、
  `catalog_facets` 至少一个 usable 目录项，即可按已有事实回答。用途匹配未被结果证明时只能按
  返回规格介绍为候选；对比字段缺失时说明限制，不为补齐信息自动重复查询。

## 7. Action Wave 与 Observation

同一个 AIMessage 可以包含多个彼此独立的 Tool Calls：

```text
AIMessage
    catalog_search(...)
    policy_search(...)
```

它们属于同一个 wave，语义是“基于同一份现有上下文规划出的并列 actions”。

如果第二个调用依赖第一个结果，例如：

```text
先搜索商品得到 sku_id
再根据 sku_id 做精确比较
```

则必须拆成两个 wave：

```text
Wave 1: catalog_search
Observation: products / sku_ids
Wave 2: catalog_compare
```

当前不构建任意 Tool DAG。

### 7.1 Subquery-aware Tool Call 与 Ledger

Orchestrator 在选择动作前先在内部把当前请求拆成可独立判断完成状态的 subquery。每个业务
Tool Call 的 LLM-facing schema 都额外要求 `subquery` 字段，用于说明该调用负责用户请求中的
哪个原子部分。该字段由 agent 层解析到 `PlannedToolCall`，不进入正式业务 Tool 参数。

`subquery_ledger` 保留每次调用的 `tool_call_id`、`tool_name`、`arguments`、`outcome`、`wave`、
`fingerprint` 和 `reused_from_tool_call_id`，并增加：

- `subquery`：该调用负责的原子需求。
- `subquery_id`：根据首次 subquery 生成的稳定身份。
- `canonical_query` / `query_fingerprint`：该 subquery 首次执行时确定的固定 query 及其指纹。
- `initial_tool_call_id`：首次确定 canonical query 的 Tool Call。
- `status`：当前调用对该 subquery 的推进状态，包括 `ready_to_answer`、`unavailable`、
  `needs_replan`、`failed`、`superseded` 和 `answered`。

`ready_to_answer` 只表示结构上存在 usable 证据，Orchestrator 仍需检查相关性和充分性。对同一
subquery 进行新一轮实质不同的调用时，旧调用标记为 `superseded`；合法终止动作引用的调用
持久化为 `answered`。Guard 和 fallback 只能引用非 `superseded` 的 active usable 结果。
fingerprint 命中只复用旧结果，不产生新的证据覆盖。

`products`、`evidence`、`order` 等兼容投影会在 Ledger 更新后根据 active Tool Result 重建，不再
由最后一次 Tool Call 无条件覆盖。因此，后续空结果不会清除其他仍然有效的 subquery 结果；同一
subquery 的新调用替代旧调用时，旧结果也不会继续进入最终回答或 working memory。

## 8. 原生 Tool Call 与结构化终态动作

需要 Tool 时，模型必须只返回供应商原生 Tool Call，`content` 为空。

不需要 Tool 时，模型必须调用一个控制 Tool，并把完整用户正文放入 `response` 参数：

```text
finish_answer(
    response="根据商品目录，目前符合条件的有……",
    used_tool_call_ids=["call-1"]
)
```

控制动作包括 `reject_out_of_scope`、`ask_clarification`、`finish_direct`、`finish_answer`、
`finish_partial`、`finish_unavailable` 和 `request_handoff`。普通文本终止会被视为无效响应。
终态通过 `terminal_guard` 校验 evidence ID 和动作前置条件；明确人工操作和明显越界请求仍由
确定性 boundary classifier 在 LLM 调用前拦截，并进入固定模板节点。

模型调用与编排流程：

```text
非流式获取完整 AIMessage
    -> native tool_call
         -> 校验并执行 Tool
    -> native control tool_call
         -> terminal_guard
         -> finalize_response / render_*_template
    -> 普通文本、空正文或非法控制动作
         -> terminal_guard replan once
         -> 仍失败时使用确定性安全 fallback
```

最终正文不会在模型生成过程中发送给用户。`finalize_response` 收到完整正文并完成持久化后，
主接口一次性返回 `ChatResponse`；兼容 SSE 接口才包装为单个完整 `delta`。这样可以避免部分正文
无法撤回，也不再因 `TYPE:` 格式偏差错误进入澄清路径。

## 9. 兼容 SSE 事件

前端默认不再调用 `/api/chat/stream`。兼容接口当前仍可发送：

| Event | 产生时机 |
| --- | --- |
| `run_started` | context 和 AgentRun 已创建 |
| `boundary` | Tool Call、最终正文或确定性边界已确定 |
| `tool_call started` | handler 执行前 |
| `tool_call completed/error` | handler 返回后 |
| `context` | 一个 Tool wave 已合并进 state |
| `delta` | `finalize_response` 完成后的完整回答或模板正文 |
| `done` | persist 完成，携带完整 `ChatResponse` |
| `error` | 运行失败或客户端取消 |

如有兼容消费者，应以 `done.response` 作为最终权威状态；当前前端直接使用 `/api/chat` 的
`ChatResponse`。

## 10. 正式 Tool Contract 接入状态

当前编排层通过：

```text
ToolContractProvider
ToolExecutor
```

获取并执行 Tool。正式实现位于 `backend/app/tools/contracts.py`：

- `DefaultToolContractProvider`：提供 Tool 模块拥有的 6 个正式 Contract。
- `RegistryToolExecutor`：校验 public input、注入可信 Runtime 字段、校验 internal input 和
  output，并调用现有 `ToolRegistry`。

`backend/app/agent/graph.py` 已直接依赖正式 Contract，不再保留 agent 层的临时 Contract 或
re-export Adapter。真实运行调用 PostgreSQL 商品/订单 Tool 和本地知识检索 Tool，不使用 mock
业务结果。

不阻塞当前运行的后续收口事项见
`docs/feature/orchestrator-requirements-for-tools.md`。

## 11. 当前 LLM-safe Tool 名称

| LLM name | 当前内部 Registry name | 事实范围 |
| --- | --- | --- |
| `catalog_search` | `catalog.search` | 商品搜索、筛选、推荐事实 |
| `catalog_compare` | `catalog.compare` | 商品对比事实 |
| `catalog_facets` | `catalog.facets` | 目录中的品牌、类目、规格字段和规格选项聚合 |
| `order_lookup` | `order.lookup` | 当前认证用户的订单和物流 |
| `policy_search` | `policy.search` | 售后、配送、发票、隐私与数据访问等政策 |
| `knowledge_search` | `knowledge.search` | 品牌、FAQ、外设和选购知识 |

响应中的兼容字段 `intent` 直接使用本轮 LLM-safe Tool name；多个 Tool 会显示例如
`catalog_search + policy_search`。该字段不参与 Graph 路由。

## 12. 当前非目标与下一步

当前未实现：

- 任意 Tool DAG。
- 在共享 AsyncSession 上并行执行 Tool。
- 写操作 Tool。
- Tool Contract 的版本协商和自动发现。
- 跨进程 MCP Tool。
- 独立 Response Generator LLM。

建议后续迭代顺序：

1. 增加真实 DeepSeek/Qwen 的完整终态 + Tool Call 联调测试。
2. 收口 Tool 注册单一事实源、稳定错误分类和 public input 未知字段校验。
3. 基于正式 `parallel_safe` 和独立 Session 策略决定是否并行执行 wave。
4. 评估 retry policy。
5. 再讨论 Working Memory 如何作为可信 context 进入 Orchestrator，避免与 Tool Contract 合并耦合。
