# 主编排进度：Plan-and-Execute + 确定性 DAG Runtime

## 1. 文档目的

本文记录当前主编排的实际实现，用于后续迭代、评审和联调。它描述的是
`backend/app/agent/` 中已经可以运行的 Graph，不是远期设想。

本阶段已把原先偏 ReAct 的 Planner / Answer Loop 收敛成职责受限的 LLM 阶段与一个确定性 Runtime：

- Request Router：先 rewrite 整体请求，再拆分用户 Goal、逐项准入，并只为允许执行的 Goal 展开
  不可变 Task DAG。
- Tool Planner：只消费 Runtime 已计算好的 ready Task，把每个 Task 编译成一个业务 Tool Call；
  不决定顺序、不新增 Task、不改写 canonical query。
- Answer Synthesizer：只消费 schema-bounded task artifacts 与 task status，回答 user-facing Task，
  并通过受限控制 Tool 终止。它不绑定业务 Tool。
- Answer Context：Runtime 先把每个 user-facing Task 的 canonical question、状态、Artifact 和
  Tool outcome 合并成 task-centered 回答记录，并计算整轮 `full / partial / none`；Answer
  Synthesizer 不再自行 join route、status、artifact 和 ledger。Context 还携带整轮
  `rewritten_query`，仅用于检查最终聚合是否完整覆盖用户目标，不作为业务事实来源。
- Zero-Tool Answer：General Direct 只回答稳定能力与寒暄，不读 history；Session Grounded 只在最近
  assistant 回答已经完整覆盖当前单 Goal 时读取 history 作答。
- DAG Runtime：负责 ready/wave、参数绑定、重试上限、Artifact 提取、blocked 传播与终态校验。

在 Request Router LLM 前还有一层确定性 fast path：只有当原始请求的每个分段都能高置信判定为
`security_refusal`、`unsupported`、`human_handoff`、`out_of_scope` 或安全的
`direct_response` 时，Runtime 才直接构造 route plan 并跳过 Router LLM。只要包含业务、歧义或
依赖上下文的分段，整轮仍交给 Router，避免把 mixed request 整体拒绝。

业务 Tool Call 不再承担 query rewrite；它来自 Tool Planner Action Compiler、确定性等价重试，
或通过安全校验的 Router capability 直达 wave。

编排层仍只通过正式 Tool Contract 使用业务能力；Catalog、Order、Knowledge 的领域查询规则留在
各自 Tool 内部，不回流到 Router 或主 Tool Planner。

## 2. 当前架构原则

主编排实现按职责拆分为以下模块，`graph.py` 只保留运行时节点协调、模型阶段选择、上下文持久化
和 API 响应组装：

- `workflow.py`：LangGraph 拓扑和条件边。
- `route_runtime.py`：Router 消息、rewrite fallback、分段、hard guard 与 route plan 投影。
- `artifacts.py`：Task status、ready 计算、确定性 Artifact Extractor 与结构化参数绑定。
- `tool_loop.py`：Action Compiler 消息、调用约束、确定性 retry 与终态决策。
- `execution.py`：业务 Tool wave 的执行、复用、事件发送和调用审计。
- `fallback_planner.py`：无 LLM 时仍遵守 route plan 的确定性 Tool 选择。
- `prompts/route_answer.py`：General Direct 与 Session Grounded 两条零 Tool 回答协议。
- `responses.py`：fallback 回答、安全/拒绝类固定终态模板、blocked notice 与 suggested actions。
- `projections.py`：usable task artifacts 到 products/order/evidence/parsed 的兼容投影。
- `events.py`：SSE event 与审计状态序列化。
- `limits.py`：Router、Planner 和 Tool wave 的共享预算。
- `boundary.py`：安全、隐私和静态能力边界的唯一规则来源；Router prompt、Runtime hard guard 和
  deterministic fallback 均从同一个 `BoundaryPolicy` 读取。

- Request Router 不绑定任何业务 Tool，只绑定一个结构化 `route_request` 输出 Tool。
- Tool Planner 在每个 wave 只看到当前 ready Task 与 6 个业务 Tool；Answer Synthesizer 始终只绑定
  终止控制 Tool，不存在同时绑定业务 Tool 与控制 Tool 的 Recovery Planner。
- 业务事实必须来自 usable task artifacts，不能由 LLM 编造。
- 正常 `empty / not_found` 是可靠的否定结论，可形成 `answered_no_match` 并作为完整回答；它不再
  与 Tool 故障或能力不支持共用“无法回答”语义。
- turn 开始时的 working memory 只作为 `working_memory_snapshot` 读取；wave 间通信只使用
  `task_status`、`task_artifacts`、`tool_results` 和 ledger。
- `intent` 不参与控制流，只作为兼容的日志和前端 tag 字段。
- Router 必须先生成整轮 `rewritten_query`，再拆成可独立验收的 Goal；不能先拆分再分别猜测原意。
- Goal 使用 `goal_n`、持有 disposition；只有 `tool_planning` Goal 才包含 Task。Task 使用 `task_n`，
  至少包含 `goal_id`、冻结的 `canonical_query`、`depends_on`、typed `input_requirements`、
  `produces` 与 `answer_role`，必要时还有确定性的 `result_selector`。
- disposition 包括 `tool_planning`、`direct_response`、`session_grounded_response`、
  `clarification`、`human_handoff`、
  `out_of_scope`、`unsupported` 和 `security_refusal`。
- mixed intent 按 subquery 分别准入；只有 `tool_planning` 项能进入 Tool Planner，其余部分由
  Runtime 在最终回答中追加确定性说明。
- 同一个 AIMessage 中的多个 Tool Calls 属于同一个 action wave。
- 每个业务 Tool Call 带一个仅供编排使用的 `subquery` 兼容元数据；它必须原样复制 `task_n` ID。
  Runtime 在调用业务 Tool 前剥离，不改变正式 Tool input contract。
- Tool Planner 只使用 Tool 的 public input schema。`catalog_search` 与 `catalog_facets` 采用
  query-first 输入，商品类目、价格、规格、facet 等结构化查询计划只由 Tool 内部 Planner 生成。
- Tool Planner 不再输出 query-first Tool 的 `query`；Runtime 根据 `subquery=task_n` 从 Task
  canonical query 派生并注入 tool query。单一 admitted task 下可确定性补全模型遗漏的 ID；多 task 仍
  要求明确 `task_n`，显式未知 ID 或把被阻断任务带入 Tool Call 时拒绝执行。
- Runtime 只调度全部 `depends_on` 已获得 usable 结果的 ready task；无依赖 task 同 wave，依赖 task
  等上游 Artifact usable 后进入下一 wave。具体 capability 通过确定性 veto 时可直接构造任意 ready wave，
  不再限定只能加速首轮。
- Result Normalizer 是无 LLM 的确定性控制层；它只判断结果结构能否作为证据，不生成客服正文。
- 全部 user-facing Task 都有 usable Artifact 时，Answer Synthesizer 只绑定 `finish_answer`；其他
  Answer Synthesizer 终态也使用 `tool_choice=required`。若模型输出不可解析结果或违规业务 Tool Call，
  Runtime 注入终止反馈并重试一次。业务恢复只允许 Runtime 对明确 `retry_once` 做一次等价重试。
- 每 turn 最多调用 1 次 Request Router；该调用不占后续主编排 LLM 预算。
- 最多执行 2 个 Tool wave，Router 之后的 Planner/Synthesizer 调用合计最多 3 次。
- DeepSeek 主链路显式使用 non-thinking mode，避免 Tool Call 后续轮次必须保存并回传
  `reasoning_content`；Qwen 分支不接收该 DeepSeek 专属参数。
- `human_handoff`、`out_of_scope`、`unsupported` 和 `security_refusal` 使用确定性模板，不采用模型
  自由生成的正文；Router 只为 clarification 生成一个受限、具体的问题。
- 若模糊的人工办理语义漏到 Answer 阶段，Answer 只能输出结构化
  `offer_handoff_confirmation=true`；Terminal Guard 校验其 completion 与 boundary 前置条件，
  Response Renderer 追加固定确认问句。整个过程不修改 boundary、不触发前端人工模式。用户下一轮
  明确确认后重新走 Router。
- Tool 参数中的认证字段由可信 Runtime 注入，不能由模型提供。
- `POST /api/chat` 是前端默认入口，通过 `AgentRuntime.run()` 一次性返回完整响应。
- SSE 接口暂时保留为兼容入口；两种接口运行同一份 LangGraph 和模型阶段选择。

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
  |-- no tool_planning Goal
  |     -> render_handoff / out_of_scope / unsupported / security / clarification
  |        / direct / session_grounded
  |     -> persist_turn
  |     -> END
  |
  `-- contains tool_planning subquery
        -> orchestrate (Router capability direct wave 或 Tool Planner)
        -> dispatch_decision

dispatch_decision
  |-- tool_calls
  |     -> execute_tool_wave
  |     -> normalize_tool_results
  |     -> extract_task_artifacts
  |     -> update_subquery_ledger
  |     -> refresh task_status / next ready wave
  |     -> orchestrate                    # Action Compiler 或 Answer Synthesizer
  |
  |-- control action
  |     -> terminal_guard
  |     |-- invalid and recoverable -> orchestrate once
  |     `-- accepted/fallback -> finalize_response / render_*_template
  |     -> persist_turn
  |     -> END
```

其中只有 `execute_tool_wave -> Artifact Store -> orchestrate` 会回到主编排循环；
`finalize_response` 和所有 Router 回答节点都直接进入 `persist_turn`。

职责上依次是 Context Assembly、Admission、Tool/Answer、Deterministic Runtime 和 Durability；
具体节点及回边以上图为准，不再维护另一份重复拓扑。

## 4. 决策模型

### 4.1 Request Router disposition

| Disposition | 含义 | 是否进入 Tool Planner |
| --- | --- | --- |
| `tool_planning` | 只读能力白名单内 | 是 |
| `direct_response` | 不依赖会话事实的身份、能力、使用方式或寒暄；进入 General Answer | 否 |
| `session_grounded_response` | 最近 assistant 回答已完整覆盖的单 Goal 追问 | 否 |
| `clarification` | 缺少安全路由或 Tool 选择所必需的信息 | 否 |
| `human_handoff` | 明确要求人工，或明确要求执行售后、身份核验、账户安全流程 | 否 |
| `out_of_scope` | 与 PC 外设商城无关 | 否 |
| `unsupported` | 商城语境内但静态白名单不支持，如取消/修改订单、代下单或代支付 | 否 |
| `security_refusal` | 其他客户数据或敏感凭证请求 | 否 |

Router 输出不是整轮单标签。若请求同时包含商品推荐和通用编程，前者为 `tool_planning`，后者为
`out_of_scope`；Tool Planner 只看到前者，最终 Runtime 再追加后者的固定说明。

### 4.2 Tool Planner / Answer Synthesizer decision

进入主 Tool loop 后，每次模型调用只能产生以下一种决策：

| Decision | 是否调用 Tool | 用户可见内容 | 后续节点 |
| --- | --- | --- | --- |
| `tool_calls` | 是 | 当前不输出正文 | `execute_tool_wave` |
| `grounded_response` | 否 | `finish_answer.response` | `terminal_guard` |
| `partial_response` | 否 | `finish_partial.response` | `terminal_guard` |
| `unavailable_response` | 否 | Runtime 按结果类型生成的不可用说明 | `terminal_guard` |
| `clarification` | 否 | `ask_clarification.response` | `terminal_guard` |

典型判断逻辑：

```text
首次处理已准入的商品、订单、物流、政策、FAQ、品牌或外设知识事实
    -> tool_calls

Answer Synthesizer 判断全部 user-facing Artifact 已充分支持回答
    -> grounded_response

Artifact 只支持部分回答
    -> partial_response

没有可用证据，或结构化错误表明仍缺必要信息
    -> unavailable_response / clarification

初始边界、直接回复和明显澄清
    -> 已由 Request Router 终止，不进入 Tool Planner
```

`direct_response` 由专用 General Answer Prompt 动态生成；`session_grounded_response` 由专用
Session Grounded Answer Prompt 基于最近 history 生成。`human_handoff`、`out_of_scope`、
`unsupported` 和 `security_refusal` 仍由确定性模板终止。无 `route_plan` 的 legacy Orchestrator
binding 已删除；Tool Planner 只处理准入后的业务 Task。

## 5. 节点说明

### 5.1 `load_context`

类型：确定性应用节点，包含 Repository I/O，不调用 LLM。

职责：

- 获取或创建当前用户的 conversation。
- 读取最近 2 个完整 `user -> assistant` 轮次（最多 4 条消息）。
- 保存当前 user message。
- 创建本轮 `AgentRun`。
- 初始化 `conversation_id`、`user_message_id`、`run_id`、history 和只读
  `working_memory_snapshot`。

该节点只组装 session context，不做意图判断或 Tool 选择；snapshot 在本 turn 内不承担 wave 间通信。

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
- 身份/能力、寒暄/感谢和商城下单指引等 `direct_response`，由专用 General Answer Prompt 回答。

fast path 使用与 post-router guard 相同的原始文本规则，并把来源记录为
`route_source=deterministic_fast_path`。它不提前处理 clarification、商品/订单/政策查询、需要
working memory 的指代或任何 mixed request；这些仍调用 Router LLM。

输入包括原始请求、最近完整历史、`working_memory_snapshot` 和显式长期偏好。Router 负责：

1. 按“当前请求 > working memory > 长期偏好 > history”融合最少必要上下文。
2. 修正常见 typo 和口语省略，但不猜测订单号、金额、型号、数量、地址或身份等关键实体。
3. 先生成整轮 `rewritten_query`，再拆成最多 8 个自包含 subquery。
4. 为每个 Goal 分配稳定 `goal_n` ID 和 disposition。
5. 只为准入 Goal 展开带稳定 `task_n` ID 的 Task DAG；对 mixed intent 不能整体放行或整体拒绝。
6. 仅当最近 assistant 已明确提供全部事实，且当前单 Goal 只需排序、筛选、比较、归纳或简单计算
   时使用 `session_grounded_response`；该 disposition 不能与其他 Goal 混用。

Router 不绑定 6 个业务 Tool，也不生成客服正文，只能调用一次 `route_request`。Runtime 之后还会
同时核对原始请求和每个 subquery，执行确定性安全、静态能力、人工接管和明显越界 guard；即使
模型在 rewrite 时遗漏风险语义，纯风险请求也会被覆盖为安全终态，mixed request 则补回相应的
blocked subquery。

对 session grounding，Runtime 还会同时扫描原始输入、rewrite 和 Goal query；出现“当前、最新、
变化、库存、物流状态”等刷新语义，或没有可用 assistant history 时，强制回退 `tool_planning`。
不确定时允许多调用一次 Tool，不允许复用可能过期的历史事实。

`rewritten_query` 和原始 `message` 同时保存在 state：原始输入用于审计和安全校验，rewrite 用于
下游规划。只有 `tool_planning` subquery 会写入 `planned_subqueries`；其余写入
`blocked_subqueries`，不会出现在 Tool Planner 输入中。

### 5.3 `dispatch_route`

类型：确定性 conditional edge。

- 没有 `tool_planning`：进入对应 Router 回答节点，不调用业务 Tool。General Direct 不接收 history；
  Session Grounded 接收最近 history 与当前 routed query，但只把历史 assistant 内容视为事实。
  安全、拒绝、越界和人工接管继续使用固定模板。
- 至少一个 `tool_planning`：进入 Tool Planner。其他 blocked subquery 暂存到最终回答阶段。

### 5.4 `orchestrate`（阶段选择器）

类型：按 state 选择 Tool Planner Action Compiler 或 Answer Synthesizer 的 LLM 节点；无 LLM key
时使用遵守冻结 Task DAG 的确定性 fallback。

生产主路径：

- 每轮先由 Runtime 从 Task DAG 计算 ready task；具体 capability 通过依赖与输入结构校验后直接构造
  wave，只有 `planner_required` 或结构不完整的 Task 才使用只绑定 6 个业务 Tool 的 Tool Planner。
- user-facing Task Artifact 全部 usable 时使用 finish-only Answer Synthesizer，只绑定
  `finish_answer`；存在空结果
  或部分结果但不需要补查时，使用绑定 4 个终止控制 Tool 的 Answer Synthesizer。两者都设置
  `tool_choice=required`，最终中文正文仍由模型直接写入控制 Tool 的 `response`。
- timeout 等 Tool 错误不再交给 LLM 恢复。只有结构化错误明确给出 `retry_once` 时，Runtime 才按
  相同 fingerprint 编译一次等价重试；其他错误直接进入 partial/unavailable 收口。
- Router 为 `tool_planning` Task 输出受限 `capability`。Runtime 校验 capability、依赖、输入来源和
  ready 状态后直接构造当前 wave，不再通过原始请求关键词或旧 intent 分类重新否决 Router；
  `planner_required`、结构不完整或不支持的 capability 回退正常 Planner。
- Planner 只接收 ready Task 的 `{id, goal_id, canonical_query, depends_on, input_requirements,
  produces, answer_role, result_selector}`，不接收原始请求、历史、working memory、长期偏好、
  Artifact、ledger 或 blocked Task。
- 为每个 ready Task 选择一个必要 Tool；`subquery` 必须复制 `task_n`，tool query 和依赖参数由 Runtime
  注入，不由 Planner 猜测。
- wave 返回后，Runtime 先执行 Normalizer 与 Artifact Extractor，再确定下一 ready wave 或结构化终止。
- Runtime 校验 Tool 名称、route ID、query 冻结、执行预算和终态协议。
- Answer Synthesizer 的结构化输出若不可解析或错误返回业务 Tool Call，Runtime 注入停止反馈并自动
  重试一次；供应商调用异常不在该结构重试范围内。

商品 Tool 内部的 `LLMCatalogQueryPlanner` / rule-based planner 属于 Tool 实现，不是 Request
Router 或主 Tool Planner。主 Planner 不生成 `category`、`filters`、`preference_defaults` 等领域
内部查询计划。

Prompt 分层代码：

```text
backend/app/agent/prompts/
├── router.py       # Router 身份、rewrite、拆分、白名单、mixed intent 与结构化输出
├── route_answer.py # General Direct / Session Grounded 零 Tool 回答协议
├── security.py     # Router 使用的权威隐私与敏感凭证规则
├── static.py       # 独立的 Action Compiler / Answer Prompt
├── tool_call.py    # Tool input 与调用纪律
├── observation.py  # Artifact 语义、场景映射和停止规则
├── response.py     # 客服口吻与 Artifact 阶段业务字段翻译
├── dynamic.py      # ready Task、Artifact、task status 与 ledger 的动态输入
└── __init__.py
```

运行时加载位置：

| 阶段 | 加载 | 明确不加载 |
| --- | --- | --- |
| Request Router | 原始请求、history、working-memory snapshot、长期偏好、rewrite/拆分/准入/安全策略 | 业务 Tool schema、Tool Result 解释、客服结果正文策略 |
| Tool Planner | 当前 ready task、精简 Tool 路由、Tool input 协议、6 个业务 Tool schema | 原始请求、history、memory、等待依赖/blocked task、客服语气、Result/恢复规则、控制 Tool schema |
| Answer Synthesizer | user-facing Task、task status、schema-bounded artifacts、ledger、客服表达、终止控制 schema | Router 原始上下文、业务 Tool schema、原始 ToolMessage、恢复规则 |
| General Direct | 当前 routed query、稳定商城能力边界 | history、业务 Tool、动态业务事实 |
| Session Grounded | 最近 history、当前 routed query、历史事实约束 | 业务 Tool、新事实、实时性声明 |

query rewrite 只发生在 Router。Action Compiler 不携带客服表达或 Artifact；Answer Synthesizer
看不到业务 Tool schema 和原始 Tool Result，只读取 Artifact Store。Router capability 通过
Runtime veto 时可跳过当前 wave 的 Tool Planner 网络往返。

Runtime 对失败使用以下确定性规则：

| recommended_action | 动态规则 |
| --- | --- |
| `retry_once` | 相同 Tool 与参数最多重试一次；相同错误失败两次后停止 |
| `replan_arguments` | 当前核心版本不交给 LLM 猜参数，停止并说明对应部分不可用 |
| `explain_temporary_unavailability` | 不重复调用同一依赖，说明暂时不可用 |
| `request_authentication` | 停止查询并要求恢复认证 |
| `stop` | 不重试，使用成功的其他结果或安全结束 |

错误文本不进入 Action Compiler。`ok=true/result_type=empty` 按已完成但不可用的结果结束；只能
建议用户下一轮明确改变条件，不能在当前 turn 静默放宽。

### 5.5 `dispatch_decision`

类型：确定性 conditional edge，不调用 LLM，不执行 Tool。

它只做第一层分流：

```text
tool_calls       -> execute_tool_wave
其他控制动作     -> terminal_guard
```

新 Router 主路径的 Planner 不再接收 handoff、out-of-scope 或 direct 控制能力；这些初始边界已经
由 `dispatch_route` 处理。Answer 控制动作先经过 `terminal_guard`。`dispatch_decision` 不重新
理解用户输入，也不重新分类 routed subquery。

### 5.6 `execute_tool_wave`

类型：确定性 action execution 节点；由工程代码调用业务 Tool。

职责：

1. 根据 LLM-safe name 查找 Tool Contract。
2. 校验 `subquery=task_n` 是当前 ready Task 或确定性 retry，而不是仍在等待依赖或已被阻断的 Task。
3. 由 Runtime 从 task canonical query 派生 `arguments.query`，并分别记录 `canonical_query` 与
   `tool_query`，不要求 Planner 逐字符复制已知文本。
4. 对依赖 Task 按 `input_requirements` 从只读 working-memory snapshot 和 task artifacts 绑定参数；
   商品比较绑定 `sku_ids`，订单依赖只在上游 Artifact 给出唯一订单 ID 时绑定。冻结 Task 已声明
   比较输入时，Runtime 不再从原始请求二次推断或追加商品。Runtime 不把上游 Tool Result 改写成
   新 query。
5. 使用 public input model 校验 LLM arguments，并规范化为 public schema 允许的字段。
6. 从可信 Runtime 注入 `user_id` 等字段。
7. 调用 `ToolExecutor`。
8. 将 handler 异常归一化为结构化 `ToolExecutionResult`。
9. 保存 Tool Call 输入和输出；wave 完成后再从 usable Artifact 投影到
   `products`、`evidence`、`order`。

同 wave 表示语义并行和相同依赖层级；当前物理执行仍串行。原因是数据库 Tool 共享 SQLAlchemy
`AsyncSession`，在 Contract 明确 `parallel_safe` 且每个并发调用使用独立 Session 之前，不使用
`asyncio.gather`。

### 5.7 `normalize_tool_results`、`extract_task_artifacts` 与 `update_subquery_ledger`

类型：确定性结果适配和状态更新节点，不调用 LLM。

`normalize_tool_results` 把 Tool execution envelope 归一化为 `usable`、`empty`、`not_found`、
`unsupported`、`insufficient` 或 `error`。`extract_task_artifacts` 对 Catalog、Compare、Facet、Order
和文档结果执行确定性 schema extractor：销量名次在这里确定性选中；文档保留原 chunk、
`evidence` 与 `source_tool_call_id`，不做自由总结。`update_subquery_ledger` 记录调用审计，随后 Runtime
从 Artifact Store 刷新 `task_status` 与下一 ready wave。
Ledger 继续处理 supersede/reuse；兼容投影只从 usable Artifact 重建。Normalizer 只判断结构
可用性，不负责客服表达；具体分类见第 7 节。

### 5.8 `terminal_guard`

类型：确定性终态校验节点，不调用 LLM。

- 校验控制动作、answerable Tool Call ID、全部初始 Task 覆盖和 partial/unavailable 前置条件；
  已有可回答 Task 时不能用澄清丢弃结果，正常无匹配也属于 answerable 的否定结论。
- 校验当前 boundary 与冻结 route plan 一致；Answer 不能改变 Router 的边界。
- `offer_handoff_confirmation=true` 只允许用于存在未完成 Task 的 partial/none，并且 route boundary
  必须保持 `in_scope_auto`。Guard 不再通过“转人工”等正文关键词推断人工确认意图。
- 非法终态在预算内最多让 Answer 重试一次；再次失败走安全 fallback。
- 接受 Answer Synthesizer 的 `finish_answer` / `finish_partial` 后把引用条目标记为 `answered`，再分发
  到对应终态节点。

### 5.9 `finalize_response`

类型：确定性 response assembly 节点，不再次调用 LLM。

Answer Synthesizer 已经通过终止控制 Tool 返回完整用户正文。该节点：

- 把完整正文写入 `state.answer`。
- 当模型正文为空时使用确定性 fallback。
- 普通 `finish_unavailable` 根据结构化 Tool outcome 渲染安全说明，不采用模型可能包含无依据事实的
  正文；`offer_handoff_confirmation=true` 时输出唯一固定确认问句。partial 的可靠正文可以保留，
  并由 Renderer 在末尾追加同一确认问句。
- Answer Synthesizer 若已经生成非空客服正文但漏掉 `finish_answer` / `finish_partial` 控制调用，
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

适用于明确要求人工，或明确要求执行售后、身份核验和账户安全流程的场景。售后政策、期限、条件
和材料咨询不会进入该节点，而是进入 `policy_search`。该节点忽略模型正文并统一设置：

- `boundary.classification = human_handoff_required`
- 固定 handoff 文案
- 人工接管 suggested action 与当前输入中可确定的订单号/请求类型

### 5.11 `render_out_of_scope_template`

类型：确定性安全终态节点。

适用于明显超出商城客服范围的请求。它忽略模型正文，使用固定 OOS 模板，并给出回到外设
商城服务范围的 suggested action。

### 5.12 其他 Router 终态回答

- `render_unsupported_template`：商城语境内、静态白名单不支持（如取消订单或代下单），使用能力
  边界模板。
- `render_security_template`：其他客户数据或敏感凭证，使用固定安全拒绝模板，零业务 Tool。
- `render_direct_template`：名字为兼容旧图保留，实际调用专用 General Answer Prompt，零业务 Tool。
- `render_session_grounded_response`：只在 Router 高置信度命中且 Runtime 刷新语义 veto 未触发时，
  将最近 history 与当前 routed query 交给专用 Answer Prompt；历史 user 消息仅用于理解指代，事实
  必须来自历史 assistant 回答，且不得把旧数据描述为当前最新。
- `render_clarification_template`：展示 Router 生成并通过 schema 校验的一条具体澄清问题。

这些节点都直接进入 `persist_turn`。`unsupported` 和 `security_refusal` 已加入后端与前端 boundary
枚举，前端统一展示 blocked 状态，但使用不同徽标和说明。

### 5.13 `persist_turn`

类型：确定性 durability 节点，包含 Repository/transaction I/O。

职责：

- 保存最终 assistant message。
- 保存 route plan、decision、boundary、Tool tag、products、evidence 和 order metadata。
- turn 开始时加载的 working memory 只作为 `working_memory_snapshot` 读取；wave 间不修改它。
- 任一合法终态形成后，`complete_turn()` 根据最终 usable Artifact 投影一次性生成 next working
  memory，并与消息、run audit 在同一事务中提交；wave 间不写 working memory。
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
working_memory_snapshot
task_status
task_artifacts
route_answer_mode
orchestrator_call_count
tool_wave_count
tool_waves
tool_results
```

最大链路为：

```text
Deterministic pre-route gate
    -> Request Router #1（仅 business/mixed/ambiguous）
    -> Router capability 直达 ready wave，或 Tool Planner #1
    -> Tool Wave #1
    -> 依赖 Task Action Compiler 或 Answer Synthesizer #2
    -> Tool Wave #2
    -> Answer Synthesizer #3
    -> 用户终态
```

约束：

- `request_router_call_count` 为实际 LLM 调用数：fast path 为 0，其余最多为 1。
- `orchestrator_call_count <= 3`，统计 Router 之后实际发生的 Tool Planner 与 Answer Synthesizer
  调用；Router capability 直达和确定性 retry 本身不计数。
- `tool_wave_count <= 2`
- Router 调用不计入 `orchestrator_call_count`，不会挤占原有 Tool loop 预算。
- 第 3 次主编排 LLM 调用不允许再发起业务 Tool Call。
- 达到限制仍请求 Tool 时，工程代码按当前 active 结果确定性结束：已有 usable 结果时生成完整或
  部分回答，没有 usable 结果时才生成 unavailable/clarification；不发起第 4 次 LLM，也不丢弃
  前序可用结果。该终止与普通请求共用相同的 answer、partial、unavailable、clarification 语义，
  用户回复不暴露调用次数、wave、预算或处理上限。
- `ok=true` 表示本次业务观察已经完成；empty、not_found、unsupported 和 insufficient 不允许在
  当前 turn 自动换 Tool、放宽条件或改写 query 重查。usable 通常直接回答，但原请求明确要求且
  依赖该结果的白名单后续步骤仍可进入下一 wave。
- 下一 wave 只允许两类动作：Runtime 允许的一次等价 retry；冻结 Task DAG 中依赖 Artifact 已满足
  的 ready Task。`catalog_compare.sku_ids` 只从 context artifact 与 task artifact 绑定；订单详情
  只在上游 Artifact 给出唯一候选 ID 时自动绑定，多候选会 blocked 并请用户选择。
- 销量第 N 名 task 使用确定性 `result_selector`。“商品/系列销量排行”默认按 SPU `sales_count`
  去重排序，再选择该 SPU 中 SKU 销量最高的代表版本用于比较；用户明确说版本/SKU 排名时才按
  `sku_sales_count`。Catalog Repository 对 SPU 排名直接按系列去重并返回每个系列的代表 SKU，
  不依赖固定扩大 TopK 或对单个系列 SKU 数量的假设。
- Router 输出后、首次 Tool Call 之前 canonical query 已经冻结；首轮 Planner 也不能改写。同一
  Task 的后续调用必须保持 `canonical_query` 不变：timeout 只能原样重试一次；当前核心版本不让
  LLM 修正 invalid_input 参数；insufficient、invalid_catalog_plan、empty 和
  usage_mapping_unavailable 都不允许改写 query 补救。用户新一轮输入可以生成新的 query。
- 推荐结果只要与用户明确条件相关并能回答核心问题，就视为充分；用户没有要求多个品牌、指定
  数量或更多备选时，不为了结果丰富度继续调用 Tool。
- `catalog_search` 至少一个相关 usable 商品、`catalog_compare` 至少两款 usable 商品、
  `policy_search` / `knowledge_search` 至少一个能直接支持核心问题的 usable chunk、
  `catalog_facets` 至少一个 usable 目录项，即可按已有事实回答。用途匹配未被结果证明时只能按
  返回规格介绍为候选；对比字段缺失时说明限制，不为补齐信息自动重复查询。

## 7. Action Wave 与 Artifact Store

Router Task DAG 先按依赖计算 wave。同一个 AIMessage 可以包含多个彼此独立的 ready task：

```text
AIMessage
    catalog_search(...)
    policy_search(...)
```

它们属于同一个 wave，语义是“依赖层级相同”，不表示当前已经物理并发执行。

如果第二个调用依赖第一个结果，例如：

```text
先搜索商品得到 sku_id
再根据 sku_id 做精确比较
```

则必须拆成两个 wave：

```text
Wave 1: catalog_search
Artifact: products / selected_sku_ids / source_tool_call_id
Wave 2: catalog_compare
```

例如“对比这个和销量第二的键盘，再推荐一个鼠标”会形成三个 task：查销量第二键盘和推荐鼠标
都无依赖，进入 Wave 1；比较 task 依赖前者，Runtime 在 Wave 1 usable 后绑定“当前商品 + 排名结果”
并进入 Wave 2；最后 Answer Synthesizer 一次汇总三个 task 的证据。

Task DAG 可以表达任意无环依赖，但当前执行仍受最多两轮 wave 预算限制。

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

Catalog 场景扩容字段由确定性 extractor 原样保留在 Artifact 中，再由 Answer Synthesizer 理解：

- `applied` 表示单品类场景规则已参与筛选/排序；`expanded` 表示本次调用已完成跨品类展开，
  不得再拆分搜索。
- `unavailable` / diagnostic `usage_mapping_unavailable`：缺少可靠场景映射，不代表无库存或依赖
  故障；它不新增 Normalizer outcome，ledger 仍按现有 empty/unsupported 信号进入 unavailable。
- `required` 是硬条件，`preferred` 只影响排序；具体理由必须由商品真实 `specs` 支持。
- `deterministic_spec_mapping` 是规则推断，不是厂商认证或数据库正式用途标签。

### 7.2 Goal、Task、Artifact 与 Ledger

Request Router 先生成 `rewritten_query`，再拆出 `goal_n` 并逐项 disposition。只有准入 Goal 才
展开 `task_n` DAG。Task 使用 `goal_id`、`depends_on`、`input_requirements`、`produces` 与
`answer_role`；没有依赖的 Task 自动组成同一 ready wave。Tool Planner 不再拆分请求；每个业务
Tool Call 的 LLM-facing schema 都额外要求 `subquery` 兼容字段，值必须是已有 `task_n`。该字段由
agent 层解析到 `PlannedToolCall`，不进入正式业务 Tool 参数。

`task_artifacts[task_n]` 保存 extractor 类型、artifact type、usable、结构化 value、evidence、
`source_tool_call_id` 与 reason；`task_status[task_n]` 保存 pending、ready、running、succeeded、
unavailable、failed 或 blocked。DAG 调度只依赖这两份 run-local 状态，不从工作记忆或自然语言
Observation 反推 wave。

`subquery_ledger` 保留每次调用的 `tool_call_id`、`tool_name`、`arguments`、`outcome`、`wave`、
`fingerprint` 和 `reused_from_tool_call_id`，并增加：

- `subquery`：兼容字段，实际记录 Router 分配的 `task_n`。
- `subquery_id`：Ledger 对 Task ID 生成的稳定内部身份。
- `canonical_query` / `query_fingerprint`：Router 冻结的 task query 及其指纹；Tool 实际输入另以
  Tool Call 的 `tool_query` 元数据审计，避免把 Tool 投影误当成 task 语义。
- `initial_tool_call_id`：首次确定 canonical query 的 Tool Call。
- `status`：当前调用对该 subquery 的推进状态，包括 `ready_to_answer`、`unavailable`、
  `needs_replan`、`failed`、`superseded` 和 `answered`。

`ready_to_answer` 只表示结构上存在 usable 证据，Answer Synthesizer 仍需检查相关性和充分性。对同一
subquery 进行新一轮实质不同的调用时，旧调用标记为 `superseded`；合法终止动作引用的调用
持久化为 `answered`。Guard 和 fallback 只能引用非 `superseded` 的 active usable 结果。
fingerprint 命中只复用旧结果，不产生新的证据覆盖。

`products`、`evidence`、`order` 等兼容投影会在 Ledger 更新后根据 active usable Artifact 重建，不再
由最后一次 Tool Call 无条件覆盖。因此，后续空结果不会清除其他仍然有效的 subquery 结果；同一
subquery 的新调用替代旧调用时，旧结果也不会继续进入最终回答或 working memory。

## 8. 原生 Tool Call 与结构化终态动作

需要 Tool 时，模型必须只返回供应商原生 Tool Call，`content` 为空。

Answer Synthesizer 不再需要业务 Tool 时，必须调用一个控制 Tool，并把完整用户正文放入
`response` 参数：

```text
finish_answer(
    response="根据商品目录，目前符合条件的有……",
    used_tool_call_ids=["call-1"]
)
```

Answer Synthesizer 控制动作仅包括 `ask_clarification`、`finish_answer`、`finish_partial` 和
`finish_unavailable`。旧 `reject_out_of_scope`、`finish_direct` 与 `request_handoff` 控制动作已经
删除。终态通过
`terminal_guard` 校验 evidence ID 和动作前置条件。若模型已生成非空客服正文但漏掉控制
调用，Runtime 只在存在 active usable Tool ID 时将其包装为 `grounded_response` 或
`partial_response`；空正文、无 usable 依据或非法 Tool Call 仍视为无效。Router 的纯终态不经过
该 guard，而是由结构化 route schema、Runtime hard guard、专用零 Tool Prompt 或确定性模板保证。

模型调用与编排流程：

```text
非流式获取完整 AIMessage
    -> native tool_call
         -> 校验并执行 Tool
    -> native control tool_call
         -> terminal_guard
         -> finalize_response / render_*_template
    -> 普通正文 + active usable Tool ID
         -> Runtime 包装为 answer / partial
         -> terminal_guard
    -> Answer Synthesizer 非法结构或违规业务 Tool Call
         -> 同阶段自动重试 once
    -> 空正文、无 usable 依据或非法控制动作
         -> terminal_guard replan/fallback
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
| `delta` | `finalize_response` 或 Router 回答节点完成后的完整正文 |
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

Tool 内部的最新运行约束是：Catalog 对用户明确要求的候选数量和销量排序做确定性覆盖；Knowledge
在 chunk 级执行 BM25/vector/RRF Top-K，Runtime 对 `policy_search` / `knowledge_search` 固定传
`limit=3`，Tool 内部最小 Top-K 为 2；返回完整命中 chunk，不再二次截成 180 字，并过滤完全落在
overlap 内的短尾 chunk。SentenceTransformer 仍按模型名在单进程内懒加载复用；详细 Tool 契约
维护在 `tooluse-tools-for-orchestrator.md`。

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

确定性回归覆盖 Router rewrite/Goal 准入、单 Goal 多 Task、mixed Goal、同 wave 独立 Task、跨 wave
依赖、Artifact 提取与绑定、销量排名、comparison context、订单候选、重试/blocked 传播和终态协议。
零 Tool 路径覆盖动态 General Direct、Session Grounded 历史推导、刷新语义 veto 和 SSE delta。
当前全量为 `466 passed, 41 skipped`，Ruff 与 `git diff --check` 通过；离线控制流测试不替代真实
DeepSeek/Qwen 回复质量评测。

当前未实现：

- 超过 2 个依赖层级的任意深度 Tool DAG；当前 Task DAG 仍受 `MAX_TOOL_WAVES=2` 约束。
- Knowledge/Policy 的 schema-limited LLM Artifact Extractor；当前核心版本直接保留命中文档与
  evidence/source_tool_call_id，使用确定性 extractor，不做自由摘要。
- Tool 的物理并发执行；当前只把无依赖 task 编入同一 wave，并在共享 AsyncSession 上顺序执行。
- 写操作 Tool。
- Tool Contract 的版本协商和自动发现。
- 跨进程 MCP Tool。

建议后续迭代顺序：

1. 增加真实 DeepSeek/Qwen 的完整终态 + Tool Call 联调测试。
2. 收口 Tool 注册单一事实源、稳定错误分类和 public input 未知字段校验。
3. 基于正式 `parallel_safe` 和独立 Session 策略决定是否并行执行 wave。
4. 用线上错误分布评估是否需要继续保留 timeout/invalid_input 的一次恢复预算。
5. 建立 Router 离线评测集，重点覆盖 typo、自由指代、mixed intent、能力白名单和安全误放行率。
