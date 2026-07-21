# 主编排进度：Request Router + 受限 Tool Planner Loop

## 1. 文档目的

本文记录当前主编排的实际实现，用于后续迭代、评审和联调。它描述的是
`backend/app/agent/` 中已经可以运行的 Graph，不是远期设想。

本阶段的核心变化是把原先承担请求理解、边界判断、query rewrite、subquery 拆分、Tool 选择和
Observation 收口的单一 Orchestrator 拆成两个认知节点：

- Request Router：任何业务 Tool 之前完成 query rewrite、上下文融合、subquery 拆分与逐项准入。
- Tool Planner：只消费已准入、query 已冻结的 subquery，负责 Tool 选择、Observation 和终止。

在 Request Router LLM 前还有一层确定性 fast path：只有当原始请求的每个分段都能高置信判定为
`security_refusal`、`unsupported`、`human_handoff`、`out_of_scope` 或安全的
`direct_response` 时，Runtime 才直接构造 route plan 并跳过 Router LLM。只要包含业务、歧义或
依赖上下文的分段，整轮仍交给 Router，避免把 mixed request 整体拒绝。

业务 Tool Call 不再承担首次 query rewrite；它只是 Tool Planner 对 routed subquery 的动作选择。

当前只改写编排层。商品、订单、知识检索、记忆、Repository、数据库模型和业务 Tool 内部
实现均保持独立。

## 2. 当前架构原则

- Request Router 不绑定任何业务 Tool，只绑定一个结构化 `route_request` 输出 Tool。
- Tool Planner 每次看到全部 6 个业务 Tool，不做渐进式披露。
- 业务事实必须来自 Tool Result，不能由 LLM 编造。
- `intent` 不参与控制流，只作为兼容的日志和前端 tag 字段。
- Router 必须先生成整轮 `rewritten_query`，再拆成自包含 subquery；不能先拆分再分别猜测原意。
- 每个 routed subquery 都有稳定 `sq_n` ID、冻结的 canonical query 和 disposition。
- disposition 包括 `tool_planning`、`direct_response`、`clarification`、`human_handoff`、
  `out_of_scope`、`unsupported` 和 `security_refusal`。
- mixed intent 按 subquery 分别准入；只有 `tool_planning` 项能进入 Tool Planner，其余部分由
  Runtime 在最终回答中追加确定性说明。
- 同一个 AIMessage 中的多个 Tool Calls 属于同一个 action wave。
- 每个业务 Tool Call 带一个仅供编排使用的 `subquery` 元数据；它必须原样复制 routed `sq_n` ID。
  Runtime 在调用业务 Tool 前剥离，不改变正式 Tool input contract。
- Tool Planner 只使用 Tool 的 public input schema。`catalog_search` 与 `catalog_facets` 采用
  query-first 输入，商品类目、价格、规格、facet 等结构化查询计划只由 Tool 内部 Planner 生成。
- Tool Planner 不再输出 query-first Tool 的 `query`；Runtime 根据 `subquery=sq_n` 注入 Router
  冻结的 canonical query。单一 admitted subquery 下可确定性补全模型遗漏的 ID；多 subquery 仍
  要求明确 `sq_n`，显式未知 ID 或把被阻断任务带入 Tool Call 时拒绝执行。
- 有依赖关系的 Tool Call 必须等前一轮 Observation 返回后，在下一次 Tool Planner 调用中生成。
- Result Normalizer 是无 LLM 的确定性控制层；它只判断结果结构能否作为证据，不生成客服正文。
- 每 turn 最多调用 1 次 Request Router；该调用不占 Tool Planner 预算。
- 最多执行 2 个 Tool wave，最多调用 3 次 Tool Planner LLM。
- `handoff`、`out_of_scope`、`unsupported` 和 `security_refusal` 使用确定性模板，不采用模型
  自由生成的正文；Router 只为 clarification 生成一个受限、具体的问题。
- Tool 参数中的认证字段由可信 Runtime 注入，不能由模型提供。
- `POST /api/chat` 是前端默认入口，通过 `AgentRuntime.run()` 一次性返回完整响应。
- SSE 接口暂时保留为兼容入口；两种接口运行同一份 LangGraph，不改变 Router 或 Planner 决策。

## 3. 总体流程图

```text
HTTP Request
  -> Pydantic validation + authenticated user
  -> load_context
  -> deterministic pre-route gate
       |-- all segments terminal/direct -> construct route_plan without LLM
       `-- business/mixed/ambiguous -> request_router LLM
  -> dispatch_route

dispatch_route
  |-- pure terminal
  |     -> render_handoff / out_of_scope / unsupported / security / clarification / direct
  |     -> persist_turn
  |     -> END
  |
  `-- contains tool_planning subquery
        -> orchestrate (Tool Planner)
        -> dispatch_decision

dispatch_decision
  |-- tool_calls
  |     -> execute_tool_wave
  |     -> normalize_tool_results
  |     -> update_subquery_ledger
  |     -> Tool Results / Observations
  |     -> orchestrate                    # Tool Planner 进入下一轮决策
  |
  |-- control action
  |     -> terminal_guard
  |     |-- invalid and recoverable -> Tool Planner once
  |     `-- accepted/fallback -> finalize_response / render_*_template
  |     -> persist_turn
  |     -> END
```

其中只有 `execute_tool_wave -> Tool Results / Observations -> orchestrate` 会回到 Tool Planner 循环；
`finalize_response` 和所有 `render_*_template` 都是进入 `persist_turn` 的终态路径。

概念上可以将它分成四层：

```text
Context Assembly
    load_context

Admission and Normalization Layer
    request_router
    dispatch_route

Tool Planning / Observation Layer
    orchestrate (Tool Planner)

Deterministic Control and Action Layer
    dispatch_decision
    execute_tool_wave
    normalize_tool_results
    update_subquery_ledger
    terminal_guard
    finalize_response
    render_*_template

Durability and Transport Layer
    persist_turn
    complete ChatResponse
    legacy SSE custom events
```

## 4. 决策模型

### 4.1 Request Router disposition

| Disposition | 含义 | 是否进入 Tool Planner |
| --- | --- | --- |
| `tool_planning` | 只读能力白名单内 | 是 |
| `direct_response` | 身份、能力、使用方式、商城服务理念或寒暄 | 否 |
| `clarification` | 缺少安全路由或 Tool 选择所必需的信息 | 否 |
| `human_handoff` | 明确要求人工，或明确要求执行售后、身份核验、账户安全流程 | 否 |
| `out_of_scope` | 与 PC 外设商城无关 | 否 |
| `unsupported` | 商城语境内但静态白名单不支持，如取消/修改订单、代下单或代支付 | 否 |
| `security_refusal` | 其他客户数据或敏感凭证请求 | 否 |

Router 输出不是整轮单标签。若请求同时包含商品推荐和通用编程，前者为 `tool_planning`，后者为
`out_of_scope`；Tool Planner 只看到前者，最终 Runtime 再追加后者的固定说明。

### 4.2 Tool Planner decision

Tool Planner 每次只能产生以下一种决策：

| Decision | 是否调用 Tool | 用户可见内容 | 后续节点 |
| --- | --- | --- | --- |
| `direct_response` | 否 | `finish_direct.response` | `terminal_guard` |
| `clarification` | 否 | `ask_clarification.response` | `terminal_guard` |
| `grounded_response` | 否 | `finish_answer.response` | `terminal_guard` |
| `partial_response` | 否 | `finish_partial.response` | `terminal_guard` |
| `unavailable_response` | 否 | Runtime 按结果类型生成的不可用说明 | `terminal_guard` |
| `handoff` | 否 | 固定模板 | `terminal_guard` |
| `out_of_scope` | 否 | 固定模板 | `terminal_guard` |
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

初始边界、能力、安全和明显澄清
    -> 已由 Request Router 终止，不进入 Tool Planner
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

### 5.2 `request_router`

类型：确定性 pre-route gate + 单次结构化 LLM 路由节点；无 LLM key 或 Router 输出非法时使用保守
rule-based fallback。

pre-route fast path 提前处理以下高置信纯终态请求：

- 其他客户订单/联系方式、密码验证码和支付凭证等 `security_refusal`。
- 取消/修改订单、代下单支付、图片/条码/文件识别、提醒/预留、联系外部快递、视频硬件诊断和
  不受支持的历史价格/趋势分析等 `unsupported`。
- 明确转人工、账户安全、登录邮箱修改、订单/账户数据导出、个人偏好或记忆删除等
  `human_handoff`。
- 售后关键词本身不触发接管：退货天数、期限、条件、材料、规则和“怎么申请”等信息问法继续
  进入 Router/`policy_search`；只有“帮我申请退货”“我要退货”“申请退货”等明确执行意图才
  进入 `human_handoff`。
- 天气、股票、医疗、通用编程、手机等明确 `out_of_scope`。
- 身份/能力、寒暄/感谢和商城下单指引等确定性 `direct_response`。

fast path 使用与 post-router guard 相同的原始文本规则，并把来源记录为
`route_source=deterministic_fast_path`。它不提前处理 clarification、商品/订单/政策查询、需要
working memory 的指代或任何 mixed request；这些仍调用 Router LLM。

输入包括原始请求、最近完整历史、`WorkingMemoryV2` 和显式长期偏好。Router 负责：

1. 按“当前请求 > working memory > 长期偏好 > history”融合最少必要上下文。
2. 修正常见 typo 和口语省略，但不猜测订单号、金额、型号、数量、地址或身份等关键实体。
3. 先生成整轮 `rewritten_query`，再拆成最多 8 个自包含 subquery。
4. 为每个 subquery 分配稳定 `sq_n` ID 和 disposition。
5. 对 mixed intent 逐项准入，不能整体放行或整体拒绝。

Router 不绑定 6 个业务 Tool，也不生成客服正文，只能调用一次 `route_request`。Runtime 之后还会
同时核对原始请求和每个 subquery，执行确定性安全、静态能力、人工接管和明显越界 guard；即使
模型在 rewrite 时遗漏风险语义，纯风险请求也会被覆盖为安全终态，mixed request 则补回相应的
blocked subquery。

`rewritten_query` 和原始 `message` 同时保存在 state：原始输入用于审计和安全校验，rewrite 用于
下游规划。只有 `tool_planning` subquery 会写入 `planned_subqueries`；其余写入
`blocked_subqueries`，不会出现在 Tool Planner 输入中。

### 5.3 `dispatch_route`

类型：确定性 conditional edge。

- 没有 `tool_planning`：按安全拒绝、人工接管、澄清、能力不支持、越界、direct 的顺序进入相应
  模板节点，不调用业务 Tool。
- 至少一个 `tool_planning`：进入 Tool Planner。其他 blocked subquery 暂存到最终回答阶段。

### 5.4 `orchestrate`（Tool Planner）

类型：LLM Tool planning / observation 节点；无 LLM key 时使用 rule-based Tool 选择 fallback。

生产主路径：

- 首轮只绑定 6 个业务 Tool；正常 Observation 只绑定 `finish_answer`、`finish_partial`、
  `finish_unavailable` 和 `ask_clarification`。
- 仅当仍有未调用的 routed subquery、可恢复失败、订单候选或明确比较依赖时，Observation 才重新
  绑定业务 Tool；无 Router 的 legacy 路径保留完整 Tool 集合作为兼容。
- 首轮只接收允许的 `{id, canonical_query}`，不接收原始请求、历史、working memory、长期偏好或
  blocked subquery。
- 为每个 routed subquery 选择一个必要 Tool；`subquery` 必须复制 `sq_n`，canonical query 由
  Runtime 注入，不由 Planner 复写。
- Tool Observation 返回后，决定受限恢复、依赖调用或结构化终止。
- Runtime 校验 Tool 名称、route ID、query 冻结、执行预算和终态协议。

商品 Tool 内部的 `LLMCatalogQueryPlanner` / rule-based planner 属于 Tool 实现，不是 Request
Router 或主 Tool Planner。主 Planner 不生成 `category`、`filters`、`preference_defaults` 等领域
内部查询计划。

Prompt 分层代码：

```text
backend/app/agent/prompts/
├── router.py       # Router 身份、rewrite、拆分、白名单、mixed intent 与结构化输出
├── security.py     # Router 使用的权威隐私与敏感凭证规则
├── static.py       # 独立的 planning / observation Prompt
├── tool_call.py    # 按阶段拆分的 Tool input 与失败恢复纪律
├── observation.py  # Tool Result 语义、场景映射和停止规则
├── response.py     # 客服口吻与 Observation 阶段业务字段翻译
├── dynamic.py      # Planner 阶段选择、执行状态、ledger 与按需失败恢复
└── __init__.py
```

运行时加载位置：

| 阶段 | 加载 | 明确不加载 |
| --- | --- | --- |
| Request Router | 原始请求、history、working memory、长期偏好、rewrite/拆分/准入/安全策略 | 业务 Tool schema、Tool Result 解释、客服结果正文策略 |
| Tool Planner 首轮 | routed subquery、精简 Tool 路由、Tool input 协议、6 个业务 Tool schema | 原始请求、history、memory、blocked subquery、客服语气、Result/恢复规则、控制 Tool schema |
| 正常 Tool observation | routed subquery、ledger、ToolMessage、Result 解释、客服表达、4 个终止控制 schema | Router 上下文、首轮 Tool 路由、业务 Tool schema、失败恢复规则 |
| 恢复/依赖 observation | 正常 Observation 内容、按需恢复规则、业务 Tool schema | Router 边界分类与 blocked subquery |

因此 query rewrite 只发生在 Router。首轮只加载 `TOOL_INPUT_PROTOCOL`；
`TOOL_RECOVERY_PROTOCOL` 只在真实失败时随动态 failure block 注入。Router 主路径不再同时发送
`planner_request` 和 `<routed_subqueries>` 两份 canonical query。原始 Tool Result 仍通过
`ToolMessage` 传递，不复制进普通用户正文。

精简后的静态输入基线（不含实际 query、history 和 Tool Result）从约 44.5k 字符降至约 18.4k：
Planning Prompt 从 9,932 降至 1,825 字符，正常 Observation Prompt 从 10,581 降至 4,109
字符，正常 Observation schema 从全部 13 个 Tool 的约 9,860 字符降至 4 个控制 Tool 的约
2,300 字符。该优化降低输入处理成本，但不会消除新增 Router 的串行网络往返。

当且仅当本轮存在 `execution.ok=false` 时，`build_orchestrator_system_prompt()` 才追加
`tool_failure_recovery`：

| recommended_action | 动态规则 |
| --- | --- |
| `retry_once` | 相同 Tool 与参数最多重试一次；相同错误失败两次后停止 |
| `replan_arguments` | 同一个 Tool 修正明确报错的非 query 参数，canonical query 不变 |
| `explain_temporary_unavailability` | 不重复调用同一依赖，说明暂时不可用 |
| `request_authentication` | 停止查询并要求恢复认证 |
| `stop` | 不重试，使用成功的其他结果或安全结束 |

动态分块只提升运行时归一化后的 Tool 名称、错误码、动作和尝试次数，不提升原始 error message，
避免把 Tool 输出中的非可信文本放进高优先级指令。`ok=true/result_type=empty` 不加载失败分块，
而是按已完成但不可用的 Observation 结束；只能建议用户下一轮明确改变条件，不能在当前 turn
静默放宽。动态分块中的“存在成功结果”以 normalized outcome 的
`has_usable_information=true` 为准，不把 `ok=true` 的空结果算作可用证据。

### 5.5 `dispatch_decision`

类型：确定性 conditional edge，不调用 LLM，不执行 Tool。

它只做第一层分流：

```text
tool_calls       -> execute_tool_wave
其他控制动作     -> terminal_guard
```

新 Router 主路径的 Planner 不再接收 handoff、out-of-scope 或 direct 控制能力；这些初始边界已经
由 `dispatch_route` 处理。Observation 控制动作先经过 `terminal_guard`；无 Router 的 legacy
兼容路径仍支持旧控制动作。`dispatch_decision` 不重新理解用户输入，也不重新分类 routed subquery。

### 5.6 `execute_tool_wave`

类型：确定性 action execution 节点；由工程代码调用业务 Tool。

职责：

1. 根据 LLM-safe name 查找 Tool Contract。
2. 校验 `subquery=sq_n` 存在于 Router 的 `tool_planning` 集合。
3. 由 Runtime 注入 `arguments.query = canonical_query`，不要求 Planner 逐字符复制已知文本。
4. 使用 public input model 校验 LLM arguments。
5. 规范化为 public schema 允许的字段，禁止把 Tool 内部规划字段重新注入调用。
6. 从可信 Runtime 注入 `user_id` 等字段。
7. 调用 `ToolExecutor`。
8. 将 handler 异常归一化为结构化 `ToolExecutionResult`。
9. 保存 Tool Call 输入和输出，并投影到 `products`、`evidence`、`order`。
10. 将完整 Tool Result 作为下一次 Tool Planner 调用的 Observation。

当前一个 wave 内串行执行 Tool。原因是数据库 Tool 共享 SQLAlchemy `AsyncSession`，在正式
Contract 明确 `parallel_safe` 且 Session 生命周期支持并发之前，不假设可以并行。

### 5.7 `normalize_tool_results` 与 `update_subquery_ledger`

类型：确定性结果适配和状态更新节点，不调用 LLM。

`normalize_tool_results` 把 Tool execution envelope 归一化为 `usable`、`empty`、`not_found`、
`unsupported`、`insufficient` 或 `error`；`update_subquery_ledger` 将 outcome 映射为 subquery 状态，
处理 supersede/reuse，并从 active 结果重建兼容投影。Normalizer 只判断结构可用性，不负责语义
相关性或客服表达；具体分类见第 7 节。

### 5.8 `terminal_guard`

类型：确定性终态校验节点，不调用 LLM。

- 校验控制动作、active usable ID、首轮 subquery 覆盖和 partial/unavailable 前置条件；已有 usable
  证据时不能用澄清丢弃结果，非法终态直接转 grounded/partial fallback。
- 没有 usable 证据时最多 replan 一次；再次失败走安全 fallback。`finish_unavailable` 正文由
  Runtime 根据具体 outcome 或场景映射诊断重写。
- 接受 `finish_answer` / `finish_partial` 后把引用条目标记为 `answered`，再分发到对应终态节点。

### 5.9 `finalize_response`

类型：确定性 response assembly 节点，不再次调用 LLM。

Tool Planner 已经返回不含 Tool Call 的完整用户正文。运行时根据是否存在成功 Tool Result，
在内部把它记录为 `direct_response` 或 `grounded_response`。该节点：

- 把完整正文写入 `state.answer`。
- 当模型正文为空时使用确定性 fallback。
- Observation 模型若已经生成非空客服正文但漏掉 `finish_answer` / `finish_partial` 控制调用，
  Runtime 会根据 active usable Tool ID 和未解决 subquery 包装为合法终态；无 usable 结果时不接受
  普通正文。多文档 fallback 只说明暂时无法可靠归纳，不再把原始文档摘要整段展示给用户。
- 对 mixed intent，将 Router 保存的 blocked subquery 按 disposition 追加固定说明；Planner 从未看到
  这些内容，也不能覆盖安全拒绝或人工接管文案。
- 生成 suggested actions。

商品 fallback 只展示用户可理解且在白名单中的规格名，未知内部 spec key 不直接透出；
`sku_sales_count` 表达为“当前版本销量”，`sales_count` 表达为“整个商品系列累计销量”。场景映射
为 `applied` / `expanded` 时使用相应的客服说明，不重复追问已知用途；
`usage_mapping_unavailable` 则说明缺少可靠的场景规格依据，不误写成无库存或系统错误。

主接口在该节点完成并持久化后返回完整 `ChatResponse`。兼容 SSE 接口只发送单个完整正文
`delta`，不发送 token/chunk 增量。

### 5.10 `render_handoff_template`

类型：确定性安全终态节点。

适用于明确要求人工，或已有人工流程的售后、身份核验和账户安全场景。它忽略模型可能生成的
正文，统一设置：

- `boundary.classification = human_handoff_required`
- 固定 handoff 文案
- 人工接管 suggested action 与当前输入中可确定的订单号/请求类型

### 5.11 `render_out_of_scope_template`

类型：确定性安全终态节点。

适用于明显超出商城客服范围的请求。它忽略模型正文，使用固定 OOS 模板，并给出回到外设
商城服务范围的 suggested action。

### 5.12 其他 Router 终态模板

- `render_unsupported_template`：商城语境内、静态白名单不支持（如取消订单或代下单），使用能力
  边界模板。
- `render_security_template`：其他客户数据或敏感凭证，使用固定安全拒绝模板，零业务 Tool。
- `render_clarification_template`：展示 Router 生成并通过 schema 校验的一条具体澄清问题。
- `render_direct_template`：身份、能力、使用方式、商城服务理念、寒暄和购买流程说明的确定性回答；
  不依赖当前业务事实，也不进入 Tool Planner。

这些节点都直接进入 `persist_turn`。`unsupported` 和 `security_refusal` 已加入后端与前端 boundary
枚举，前端统一展示 blocked 状态，但使用不同徽标和说明。

### 5.13 `persist_turn`

类型：确定性 durability 节点，包含 Repository/transaction I/O。

职责：

- 保存最终 assistant message。
- 保存 route plan、decision、boundary、Tool tag、products、evidence 和 order metadata。
- 完成 `AgentRun` 并保存可 JSON 序列化的最终 state。
- commit transaction。

只有形成合法终态后才进入该节点。

## 6. Tool Loop 与预算控制

状态中显式记录：

```text
request_router_call_count
route_source
route_plan
rewritten_query
planned_subqueries
blocked_subqueries
orchestrator_call_count
tool_wave_count
tool_waves
tool_results
```

最大链路为：

```text
Deterministic pre-route gate
    -> Request Router #1（仅 business/mixed/ambiguous）
    -> Tool Planner #1
    -> Tool Wave #1
    -> Tool Planner #2
    -> Tool Wave #2
    -> Tool Planner #3
    -> 用户终态
```

约束：

- `request_router_call_count` 为实际 LLM 调用数：fast path 为 0，其余最多为 1。
- `orchestrator_call_count <= 3`
- `tool_wave_count <= 2`
- Router 调用不计入 `orchestrator_call_count`，不会挤占原有 Tool loop 预算。
- 第 3 次 Tool Planner 不允许再发起 Tool Call。
- 达到限制仍请求 Tool 时，工程代码按当前 active 结果确定性结束：已有 usable 结果时生成完整或
  部分回答，没有 usable 结果时才生成 unavailable/clarification；不发起第 4 次 LLM，也不丢弃
  前序可用结果。该终止与普通请求共用相同的 answer、partial、unavailable、clarification 语义，
  用户回复不暴露调用次数、wave、预算或处理上限。
- `ok=true` 表示本次业务观察已经完成；empty、not_found、unsupported 和 insufficient 不允许在
  当前 turn 自动换 Tool、放宽条件或改写 query 重查。usable 通常直接回答，但原请求明确要求且
  依赖该结果的白名单后续步骤仍可进入下一 wave。
- 下一 wave 只允许两类动作：错误协议允许的同 Tool 恢复；原始请求已经明确要求且依赖上一结果
  的白名单后续步骤。`catalog_compare` 要求原始中英文请求明确包含比较意图，且至少两个整数
  `sku_ids` 全部来自 active `catalog_search` 结果；订单详情只能使用上一轮
  `order_candidates` 实际返回的 `order_id`。其他 Tool 组合不自动升级为依赖调用。
- 排名比较请求中的显式数量（如“两款”“2 个”“top 2 products”）由 Catalog Runtime 确定性
  覆盖模型生成的 limit；“销量最高”“按销量”“最畅销/最热销”确定性映射为 `sort=sales`。
  比较对象按 SKU/当前版本计算，不按 SPU 系列去重；不同颜色或版本可以作为两个独立比较项。
- Router 输出后、首次 Tool Call 之前 canonical query 已经冻结；首轮 Planner 也不能改写。同一
  subquery 的后续调用必须保持 `canonical_query` 不变：timeout 只能原样重试一次；
  invalid_input 只能由同一个 Tool 修改错误明确指出的非 query 参数；insufficient、
  invalid_catalog_plan、empty 和 usage_mapping_unavailable 不允许由 Tool Planner 改写 query
  补救。用户新一轮输入可以在明确同意改变条件后生成新的 query。
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

### 7.1 Result Normalizer 与场景映射语义

Normalizer 按固定顺序读取 `execution.ok`、`output.result_type`、`query_plan`、`diagnostics` 和实际
事实集合，不调用 LLM：

| 条件 | outcome → ledger status |
| --- | --- |
| `ok=false` / execution envelope 非法 | `error → failed` |
| 缺结构化 output、result type 与集合错配、compare 仅一款、无商品的 invalid plan | `insufficient → needs_replan` |
| 明确 unsupported 信号 | `unsupported → unavailable` |
| 合法空集合 / 订单未找到 | `empty|not_found → unavailable` |
| 搜索/facet/文档非空、compare 至少两款、单笔订单或非空候选 | `usable → ready_to_answer` |

`needs_replan` 只是分类名称，不授权改写 query 重查。

Catalog 场景扩容通过原始 Tool Result 交给 observation Prompt 理解，不把具体规格映射复制进
Tool Planner：

- `applied` 表示单品类场景规则已参与筛选/排序；`expanded` 表示本次调用已完成跨品类展开，
  不得再拆分搜索。
- `unavailable` / diagnostic `usage_mapping_unavailable`：缺少可靠场景映射，不代表无库存或依赖
  故障；它不新增 Normalizer outcome，ledger 仍按现有 empty/unsupported 信号进入 unavailable。
- `required` 是硬条件，`preferred` 只影响排序；具体理由必须由商品真实 `specs` 支持。
- `deterministic_spec_mapping` 是规则推断，不是厂商认证或数据库正式用途标签。

### 7.2 Routed Subquery、Tool Call 与 Ledger

Request Router 先生成 `rewritten_query`，再拆出可独立判断完成状态的 subquery，并分配稳定的
`sq_n`。Tool Planner 不再拆分请求；每个业务 Tool Call 的 LLM-facing schema 都额外要求
`subquery` 字段，值必须是已有 routed `sq_n`。该字段由 agent 层解析到 `PlannedToolCall`，不进入
正式业务 Tool 参数。

`subquery_ledger` 保留每次调用的 `tool_call_id`、`tool_name`、`arguments`、`outcome`、`wave`、
`fingerprint` 和 `reused_from_tool_call_id`，并增加：

- `subquery`：Router 分配的 `sq_n`。
- `subquery_id`：Ledger 对 `sq_n` 生成的稳定内部身份。
- `canonical_query` / `query_fingerprint`：Router 冻结并在首次执行时登记的 query 及其指纹。
- `initial_tool_call_id`：首次确定 canonical query 的 Tool Call。
- `status`：当前调用对该 subquery 的推进状态，包括 `ready_to_answer`、`unavailable`、
  `needs_replan`、`failed`、`superseded` 和 `answered`。

`ready_to_answer` 只表示结构上存在 usable 证据，Tool Planner 仍需检查相关性和充分性。对同一
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

新 Router 主路径的 Observation 控制动作仅包括 `ask_clarification`、`finish_answer`、
`finish_partial` 和 `finish_unavailable`。`reject_out_of_scope`、`finish_direct` 与
`request_handoff` 只保留在无 Router 的 legacy binding，不会随正常 Planner 请求发送。普通文本
终止会被视为无效响应。终态通过 `terminal_guard` 校验 evidence ID 和动作前置条件；Router 的纯
终态不经过 Tool Planner guard，而是由结构化 route schema、Runtime hard guard 和确定性模板保证。

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
| `boundary` | Request Router 完成逐 subquery 准入后 |
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
  output，并调用 `ToolCatalog` 中绑定的业务 handler。

`backend/app/agent/graph.py` 已直接依赖正式 Contract，不再保留 agent 层的临时 Contract 或
re-export Adapter。真实运行调用 PostgreSQL 商品/订单 Tool 和本地知识检索 Tool，不使用 mock
业务结果。Tool Contract/Result 保持字段结构的单一事实源；Normalizer 只提炼控制状态，Prompt
只解释跨 Tool 的通用语义和用户表达，不复制每个场景的具体 required/preferred 参数表。

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

## 12. 验证、非目标与下一步

确定性回归覆盖 pre-router fast path、Router rewrite/拆分/准入、Prompt 阶段、Normalizer、ledger、terminal guard、客服
fallback 和第二轮白名单。Router 专项测试还覆盖模型误放行第三方数据时的 Runtime hard guard、
Runtime query 注入、未知 subquery ID 拒绝、blocked subquery 上下文隔离、纯终态请求零 Router
LLM/零业务 Tool、`human_handoff` 穿过完整编译图进入接管模板，以及 mixed intent 仍调用 Router
并只执行允许部分。
20 个 Tool observation 案例经过 `execute -> normalize -> ledger -> terminal/follow-up guard`，覆盖
6 个 Tool、场景映射、不可用结果、订单候选和可恢复错误，20/20 通过；全量为
`429 passed, 41 skipped` 且 Ruff 通过。该离线控制流验证不替代真实 LLM 回复质量评测。

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
4. 用线上错误分布评估是否需要继续保留 timeout/invalid_input 的一次恢复预算。
5. 建立 Router 离线评测集，重点覆盖 typo、自由指代、mixed intent、能力白名单和安全误放行率。
