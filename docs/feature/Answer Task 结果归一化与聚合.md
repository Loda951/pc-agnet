# Answer Task 结果归一化与聚合

## 目标

Tool Planning 和 Tool 执行完成后，Answer Synthesizer 不再自行关联 route plan、task status、
artifact 和 ledger。确定性 Runtime 先把每个 user-facing Task 投影成一个回答记录，再计算整轮完成度，
Answer 只负责按记录生成客服正文。

本改造不扩大客服业务边界，不新增业务写操作。

## 整轮回答目标

`answer_context.rewritten_query` 保存 Router 完成上下文融合后的整轮语义目标。Answer 在逐 Task
判断前先读取它，并在生成正文后用它检查是否完整覆盖用户这一轮想问的内容。

`rewritten_query` 只指导聚合和表达，不能作为业务事实来源，不能覆盖、补写或改变 Task 与 Tool
Result。具体能回答什么仍以逐 Task 的 `semantic_outcome`、`artifact.facts` 和
`response_contract` 为准。

## Task 语义结果

Runtime 为每个 user-facing Task 生成：

- `question`：Router 冻结的 canonical query；
- `semantic_outcome`：该 Task 的业务回答状态；
- `artifact.facts`：可用于回答的结构化事实；
- `response_contract`：该 Artifact 类型必须回答、可以补充和禁止表达的内容；
- `explanation`：未完成时面向 Answer 的可靠原因；
- `source_tool_call_id`：事实或否定结论的来源。

当前语义结果：

| semantic outcome | 含义 | 是否算已解决 |
| --- | --- | --- |
| `answered_with_facts` | Tool 返回可直接支持 Task 的事实 | 是 |
| `answered_no_match` | Tool 正常完成，结论为无匹配或未找到 | 是 |
| `needs_clarification` | 缺少用户能够提供的必要信息 | 否 |
| `unsupported_capability` | Tool 或数据能力不支持 | 否 |
| `temporarily_unavailable` | Tool 或依赖暂时不可用 | 否 |
| `insufficient_evidence` | Observation 不足以支持可靠结论 | 否 |
| `blocked_dependency` | 上游 Task 未产生可用 Artifact | 否 |
| `incomplete` | 尚未形成可回答终态 | 否 |

正常空结果是可靠的否定答案，不再等同于系统不可用。例如“有没有十元以内的 4K 显示器”查询为空，
应该完整回答“当前没有找到”，并可使用 `finish_answer`。

## 整轮聚合

Runtime 根据全部 user-facing Task 计算：

- `full`：所有 Task 都是 `answered_with_facts` 或 `answered_no_match`；
- `partial`：至少一个 Task 已解决，同时存在未解决 Task；
- `none`：没有 Task 得到可回答结论。

控制动作建议：

- `full -> finish_answer`
- `partial -> finish_partial`
- `none + 全部 needs_clarification -> ask_clarification`
- 其他 `none -> finish_unavailable`

Answer 必须先逐 Task 回答，再按整轮完成度聚合。部分回答先展示已解决事实，再逐项解释未解决原因。

## Artifact 回答契约

`answer_context.tasks[*].response_contract` 把 Artifact 的结构语义转换为明确回答义务。

例如 `catalog_facets` 的品牌查询：

- 必须列出 `items[*].value`；
- `count` 只能解释为每个选项对应的 SKU 记录数；
- 禁止只求和 `count` 而省略品牌；
- 禁止把 `count` 总和称为品牌数或商品系列数。

商品、比较、订单和文档 Artifact 也分别约束推荐依据、比较字段、订单只读边界和文档事实范围。

## 顾客表达

Answer 保留商城客服身份、自然耐心的语气和业务结果表达规则。面向顾客默认使用日常购物语言，
不直接输出 SPU、SKU 等内部专业缩写，也不展示 `spu_id`、`sku_id` 等内部标识；对应概念分别
表达为“整个商品系列”和“当前版本/具体版本”。只有用户明确询问术语含义时，才可提及缩写并立即
用通俗语言解释。

## Answer 阶段的人工确认

明确的人工办理、写操作和安全请求仍由 Router 与 Runtime hard guard 在 Tool 前阻断，并使用原有
boundary 和固定模板。

如果请求已经进入 Answer 阶段，只能说明前面的语义较模糊。此时 Answer：

- 不得把 boundary 改为 `human_handoff_required`；
- 不得触发前端人工模式或生成 handoff action；
- 不得声称已经转接、记录、提交或办理；
- 仅在未完成 Task 看起来可能要求人工办理时，设置
  `offer_handoff_confirmation=true`，不自行生成确认问句；
- Terminal Guard 只校验该结构化信号是否适用于当前 completion 和 boundary，不解析正文关键词；
- Response Renderer 使用唯一固定问句询问用户是否需要转人工；
- 等待用户下一轮明确确认，再由正常 Router 路径处理。

因此模糊人工意图仍保持 `in_scope_auto`，前端不会自动切换模式。

## 代码位置

- `backend/app/agent/answer_context.py`：Task 语义结果和整轮聚合。
- `backend/app/agent/prompts/static.py`：Answer 两阶段处理和终止协议。
- `backend/app/agent/prompts/observation.py`：语义结果解释。
- `backend/app/agent/prompts/response.py`：客服表达与字段翻译。
- `backend/app/agent/outcomes.py`：answerable Tool Call 和终态校验。
- `backend/app/agent/responses.py`：晚期人工确认的固定问句渲染。
