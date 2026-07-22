# Tooluse Tools 接入说明

本文档面向主流程编排同学，说明当前分支提供的进程内业务 tools、输入输出、数据来源和使用边界。

## 总体边界

- ToolRegistry 提供 tool 注册、schema 校验、执行和结构化返回，AgentRuntime 负责选择、调用顺序、结果融合和最终回答。
- AgentRuntime 通过统一 contract/executor 接入全部只读业务工具；LLM-safe name 在 contract 边界映射到对应 ToolRegistry name，同步与 SSE 路径共用同一 LangGraph。
- 当前不提供 MCP。
- 商品和订单 tools 读取 PostgreSQL。
- 政策和知识 tools 读取本地 JSON 文档，并使用 BM25 / vector / hybrid 检索。
- 向量检索使用本地真实 embedding 模型 `BAAI/bge-small-zh-v1.5`，通过 `sentence-transformers` 在本机生成向量。
- 向量索引持久化在本地 JSON 文件中，不写 PostgreSQL，不写 Chroma，不依赖外部 embedding API key。
- `policy.search` / `knowledge.search` 已成为 AgentRuntime 的知识工具主链；上下文与记忆 M2 复用该链路，不再维护独立的 `knowledge.retrieve` 编排旁路。

## 主流程入参建议：query-first

- 主流程首要负责选对 tool，默认只传 `query`，不强行填写 `category`、`facet`、`filters`、
  `comparison_fields` 等内部复杂字段。
- `catalog.search`：优先传 `{"query":"用户原话","limit":3}`；类目、品牌、预算、规格和用途
  场景由 Tool 内部 `ProductQueryPlan` 解析并经过白名单校验。
- `catalog.compare`：优先传 `query`；只有当上下文已有明确 SKU 时才传 `sku_ids`。
- `catalog.facets`：Tool 会从 `query` 推断 `facet`、`category`、`brand` 和 `spec_key`；主流程
  不需要自行猜测 `facet`。
- `order.lookup`：`user_id` 只能由 Runtime 注入；Tool 可以从 `query` 提取明确长数字订单号，
  主流程已稳定拿到 `order_id` 时也可以直接传入。
- `policy.search` / `knowledge.search`：Runtime 保持 query-first，并统一传入 `limit=3`；
  该 limit 表示返回的 Top-K chunk 数量。

## ToolRegistry

入口：

```python
from app.tools.registry import build_tool_registry

registry = build_tool_registry(session)
result = await registry.execute("catalog.search", {"query": "wireless mouse", "limit": 3})
```

注册的 tools：

- `catalog.search`
- `catalog.compare`
- `catalog.facets`
- `order.lookup`
- `policy.search`
- `knowledge.search`

统一返回：

```json
{
  "tool_name": "catalog.search",
  "ok": true,
  "output": {},
  "error": null
}
```

失败时：

```json
{
  "tool_name": "missing.tool",
  "ok": false,
  "output": null,
  "error": {
    "code": "unknown_tool",
    "message": "unknown tool",
    "retryable": false,
    "recommended_action": "stop"
  }
}
```

Stable error codes exposed to orchestrator:

- `unknown_tool`: no retry, `recommended_action=stop`.
- `invalid_input`: argument validation failed, retryable via `recommended_action=replan_arguments`.
- `unauthorized`: missing authenticated runtime context, `recommended_action=request_authentication`.
- `forbidden`: explicitly forbidden, no retry, `recommended_action=stop`.
- `timeout`: tool execution timed out, retryable via `recommended_action=retry_once`.
- `dependency_unavailable`: PostgreSQL, embedding, or local index dependency unavailable, `recommended_action=explain_temporary_unavailability`.
- `execution_error`: unclassified internal failure, no Python exception class, connection string, local path, secret, or stack trace is exposed, `recommended_action=stop`.


## catalog.search

用途：

- 根据用户自然语言商品需求返回推荐商品列表。
- 适合商品推荐、商品筛选、预算查询、品牌/类目/规格过滤。

数据来源：

- PostgreSQL。
- 主要表：`sku`, `spu`, `brand`, `category`, `attribute_key`, `attribute_value`, `goods_attribute_relation`。

Orchestrator 可见的 public input：

```json
{
  "query": "Logitech wireless mouse under 300",
  "limit": 3
}
```

以下是 Tool 内部 Planner 使用的 internal input，不向 Orchestrator 暴露：

```json
{
  "query": "Logitech wireless mouse under 300",
  "category": "mouse",
  "brand": "Logitech",
  "brands": ["Logitech"],
  "excluded_brands": [],
  "excluded_usage": [],
  "min_price": null,
  "max_price": 300,
  "filters": {
    "wireless": "wireless"
  },
  "keywords": [],
  "usage": null,
  "sort": "recommend",
  "preference_defaults": {
    "brands": [],
    "excluded_brands": [],
    "excluded_usage": [],
    "max_price": null,
    "connection_type": null,
    "usage": null
  },
  "limit": 3
}
```

内部字段说明：

- `query`：必填，自然语言需求。
- `category`：可选，类目过滤。
- `brand`：可选，品牌过滤。当前 planner 会识别常见品牌；repository 层主要通过 query token 命中品牌。
- `brands` / `excluded_brands`：当前轮明确包含或排除的品牌；显式排除优先于 planner 正向推断。
- `excluded_usage`：当前轮或上下文需要排除的用途，如 `gaming`。
- `preference_defaults`：由 Agent 上下文传入的 working/长期默认值，只补当前请求未明确的字段。
- `min_price` / `max_price`：可选，价格范围。
- `filters`：可选，规格过滤，例如 `wireless`, `connection_type`。
- `keywords`：Tool Planner 的补充关键词；不向主 Orchestrator 暴露。
- `usage`：显式用途覆盖；正常 query-first 路径由 Planner 生成 `usage_scenario`。
- `sort`：Planner 可生成的排序意图。当前 Repository 的稳定排序仍以匹配分、SKU 销量、库存和
  价格为准，主 Orchestrator 不应把它描述成已经严格执行的价格/销量排序。
- `limit`：默认 `3`，范围 `1..20`。

主编排不得生成上述内部结构化字段；完整约束留在 public `query`，由 Tool Planner 统一解析。

输出：

```json
{
  "result_type": "products",
  "products": [
    {
      "spu_id": 1,
      "sku_id": 86,
      "title": "Logitech ...",
      "brand": "Logitech",
      "category": "mouse",
      "price": "197.00",
      "stock": 74,
      "sales_count": 213,
      "specs": {},
      "image_url": null
    }
  ],
  "ranking_strategy": "match_score_sales_stock_price",
  "query_plan": {
    "query": "Logitech wireless mouse under 300",
    "category": "mouse",
    "brands": ["Logitech"],
    "min_price": null,
    "max_price": 300,
    "filters": {
      "wireless": "wireless"
    },
    "keywords": [],
    "usage_scenario": null,
    "usage_mapping": {},
    "sort": "recommend",
    "limit": 3,
    "supported": true,
    "unsupported_reason": null,
    "planner": "rule_based",
    "fallback_reason": null,
    "normalization_debug": {},
    "error_type": null
  },
  "diagnostics": [
    {
      "code": "ok",
      "severity": "info",
      "message": "Catalog query completed successfully.",
      "recommended_action": "use_result",
      "details": {}
    }
  ]
}
```

排序策略：

- 匹配分优先。
- 场景 mapping 的 `preferred` 条件会增加匹配分。
- 同分时按 `sku_sales_count` 降序。
- 再按有库存优先。
- 再按价格升序。
- 最后按标题稳定排序。

边界：

- 当前已使用受控 `ProductQueryPlan` 作为中间层；默认 planner 会在有 key 时走真实 LLM，无 key 时回退规则版。
- 已提供 `LLMCatalogQueryPlanner`；它调用 LLM 生成同结构 JSON，运行时默认启用，失败时自动 fallback 到规则 planner。
- Tool 不执行 LLM 直接生成的 SQL；Python 会先校验 QueryPlan，再用 SQLAlchemy 查询 PostgreSQL。
- planner 异常或输出非法字段时，会 fallback 到规则 planner，并在 `query_plan.fallback_reason` 中返回原因。
- 超出商品表能力的问题会返回 `result_type = "empty"`、`ranking_strategy = "unsupported_query"`，并在 `query_plan.unsupported_reason` 中说明原因。
- 不直接承诺商品一定可买，最终库存以下单页为准。
- 无结果时返回 `result_type = "empty"`。

### 受控用途场景与确定性规格映射（2026-07-21）

`catalog.search` 当前只接受以下标准用途场景：

- `office`
- `gaming`
- `video_meeting`
- `live_streaming`

LLM Planner Prompt 和 Rule-based Planner 使用同一组中英文别名。未知自由值会在 Query Guard
阶段被拒绝，不能以 `supported=true` 进入执行层。当前没有数据库正式用途标签；以下结果全部属于
`deterministic_spec_mapping`，不能描述为“商品已被数据库标记为办公/游戏商品”。

#### 已指定 category

Tool 按 `usage_scenario + category` 选择版本化规则。规则分为：

- `required`：必须满足，用于候选过滤；
- `preferred`：满足时增加排序分，不代表未满足的商品绝对不适合该场景；
- 支持的操作符：`exact`、`eq`、`in`、`gte`、`lte`。

当前 `v1` 覆盖：

- `office`：keyboard、monitor、headset、webcam；
- `gaming`：mouse、keyboard、headset、monitor、speaker；
- `video_meeting`：webcam、headset；
- `live_streaming`：webcam。

应用成功时：

```json
{
  "usage_scenario": "office",
  "usage_mapping": {
    "status": "applied",
    "source": "deterministic_spec_mapping",
    "rule_version": "v1",
    "scenario": "office",
    "category": "keyboard",
    "required": [],
    "preferred": [
      {"key": "switches", "operator": "exact", "values": ["静音红轴"]}
    ]
  }
}
```

已应用 mapping 后，Repository 不再要求商品标题中出现“办公”“游戏”等文字；用途必须通过规则
过滤或排序真实生效。

#### 未指定 category

例如用户说“推荐办公相关产品”，Planner 会得到 `usage_scenario=office, category=null`。Tool 不再
返回 `usage_mapping_unavailable`，而是展开为 office 已配置的 keyboard、monitor、headset、
webcam 四条查询：

```text
office + keyboard
office + monitor
office + headset
office + webcam
```

生产路径为每个品类创建独立 SQLAlchemy `AsyncSession`，以最大并发 3 执行；不能让多个并发任务
共享请求级 `AsyncSession`。结果按品类 round-robin 合并，先保证跨品类多样性，再补充下一轮商品，
最终数量仍服从 public input 的 `limit`。

以下为结构示例；为控制篇幅省略了 `category_rules` 中每个品类的完整 `required/preferred` 内容，
真实 Tool Result 会逐品类返回完整 mapping。

```json
{
  "ranking_strategy": "scenario_category_diversified_mapping",
  "query_plan": {
    "usage_scenario": "office",
    "usage_mapping": {
      "status": "expanded",
      "source": "deterministic_spec_mapping",
      "rule_version": "v1",
      "scenario": "office",
      "category": null,
      "categories": ["keyboard", "monitor", "headset", "webcam"],
      "execution": "parallel_independent_sessions",
      "category_rules": {}
    }
  }
}
```

这里的“并行”是一次 `catalog_search` 内部的只读品类查询并行，不表示 Tool Contract 本身已经可以
被主 Orchestrator 任意并发调用。

#### 未配置的 scenario + category

例如当前 `office + mouse` 没有静音按键等可靠数据库字段，Tool 不会忽略 office 后返回普通鼠标：

```json
{
  "result_type": "empty",
  "query_plan": {
    "usage_scenario": "office",
    "usage_mapping": {
      "status": "unavailable",
      "rule_version": "v1",
      "scenario": "office",
      "category": "mouse"
    },
    "error_type": "usage_mapping_unavailable"
  },
  "diagnostics": [
    {
      "code": "usage_mapping_unavailable",
      "recommended_action": "explain_limitation_and_ask_for_concrete_preferences"
    }
  ]
}
```

`excluded_usage` 目前仍然是基于商品标题、品类和规格文本的排除启发式，不等同于正式用途标签，
也没有复用正向场景 mapping。

## 主 Orchestrator 对用途映射的理解与处理要求

主 Orchestrator 调用 `catalog_search` 时仍只传用户原始 `query` 和 `limit`，不得自行构造内部
`usage_scenario`、`required`、`preferred` 或数据库规格规则。Tool 返回后必须同时读取：

- `result_type`；
- `query_plan.usage_scenario`；
- `query_plan.usage_mapping.status/source/rule_version`；
- `query_plan.error_type`；
- `diagnostics[*].code/recommended_action/details`。

必须按以下语义处理：

1. **`status=applied`**：说明单品类确定性规格 mapping 已实际进入过滤或排序。回答应使用
   “根据该场景的规格偏好/要求筛选”之类的措辞，并只引用 Tool Result 中真实返回的规格。
2. **`status=expanded`**：说明 Tool 已在一次调用内完成跨品类展开、并行查询和多样化聚合。
   Orchestrator 应按商品 category 组织回答，不要为了同一宽泛请求再拆成多次重复
   `catalog_search`，除非用户后续指定某一品类或需要更多结果。
3. **`status=unavailable` / `diagnostics.code=usage_mapping_unavailable`**：说明当前数据库规格不足以
   支撑该场景与品类组合，不等于“没有库存”或“系统错误”。应解释能力边界，并询问用户是否愿意
   改用无线、重量、价格、轴体等已有具体条件，或改看已配置的其他品类。
4. **`result_type=empty` 且 mapping 已 applied/expanded**：说明规则已执行，但叠加品牌、预算、
   required 等条件后没有匹配商品。不得静默去掉用途条件重新查询普通商品；可以建议放宽条件，
   需要重查时先取得用户同意或保留用户明确约束。
5. **区分 `required` 与 `preferred`**：`required` 可以表述为本次筛选必须满足的条件；
   `preferred` 只能表述为排序偏好，不能据此声称所有返回商品都拥有该规格，也不能把未命中偏好
   解释成商品不适合该场景。
6. **不得伪造用途标签**：当 `source=deterministic_spec_mapping` 时，不得说“数据库标注该商品为
   办公/游戏商品”“这是官方场景认证”。应说明这是依据现有规格规则得到的推荐。
7. **遵循业务错误而非系统重试**：`usage_mapping_unavailable`、`empty_result`、
   `unsupported_query` 都可能在 Tool Contract `ok=true` 时出现。Orchestrator 必须依据 diagnostics
   处理，不能因为 `ok=true` 就把结果当成成功商品列表，也不能对确定性业务空结果机械重试。

推荐回答示例：

```text
applied：这些键盘是根据办公场景偏好的静音红轴规格优先推荐的，实际商品并没有正式“办公”标签。

expanded：我按办公场景分别从键盘、显示器、耳机和摄像头中筛选了代表性商品，下面按品类列出。

unavailable：当前商品数据没有能可靠判断办公鼠标是否静音的按键噪声规格；如果你愿意，我可以改按
无线方式、重量或预算继续筛选。
```

## 销量字段语义

- `sku_sales_count` 表示 SKU 级销量，用于比较不同颜色、版本或具体 SKU 的热度。
- `sku_sales_count_scope` 固定为 `sku`，明确该销量属于当前 SKU。
- `sales_count` 保留为 SPU 级汇总销量，表示同一个 SPU 系列下所有 SKU 的汇总。
- `sales_count_scope` 固定为 `spu`，不能用 `sales_count` 判断某个颜色或版本卖得更好。
- 当用户问“哪个版本/颜色销量更高”时，主流程应优先使用 `sku_sales_count`；如果只有 `sales_count` 而没有 SKU 级销量，则不应做 SKU 热度比较。
- 当前推荐排序和 compare 召回已改为优先使用 `sku_sales_count`，不再用 SPU 汇总销量代替 SKU 销量。

## catalog.compare

用途：

- 根据自然语言或指定 SKU 列表返回商品对比事实。
- 适合“对比 A 和 B”“哪个更适合 FPS”“帮我比较两款键盘”等场景。

数据来源：

- PostgreSQL。
- 复用商品查询能力。

输入：

```json
{
  "query": "Compare Logitech G502 and Razer Viper for FPS",
  "sku_ids": [],
  "limit": 5
}
```

字段说明：

- `query`：必填，自然语言对比需求。
- `sku_ids`：可选，主流程如果已经拿到明确 SKU，可以直接传入。
- `limit`：默认 `5`，范围 `2..10`。

输出：

```json
{
  "result_type": "comparison",
  "products": [
    {
      "sku_id": 86,
      "spu_id": 1,
      "title": "Logitech ...",
      "brand": "Logitech",
      "category": "mouse",
      "price": "197.00",
      "stock": 74,
      "sales_count": 213,
      "specs": {},
      "image_url": null
    }
  ],
  "comparison_fields": ["price", "stock", "brand", "category"],
  "missing_fields": {},
  "query_plan": {}
}
```

边界：

- Tool 只返回事实依据，不做最终购买承诺。
- 当前自然语言路径会生成 `CatalogComparePlan`，识别候选对象、品牌、类目、对比字段和使用场景。
- 已提供 LLM compare planner；运行时默认启用，失败时仍可 fallback 到规则 planner。
- 对比字段会经过白名单校验，只允许基础字段和商品规格白名单字段。
- 无结果时返回 `result_type = "empty"`。

## order.lookup

用途：

- 查询用户订单。
- 支持两种路径：按用户查最近订单列表，或按 `user_id + order_id` 查单个订单详情。

数据来源：

- PostgreSQL。
- 主要表：`app_user`, `order_info`, `order_item`, `order_logistics`。

Orchestrator 可见的 public input：

```json
{
  "query": "查询订单 202607020001 的物流",
  "order_id": 202607020001,
  "limit": 5
}
```

字段说明：

- `query`：可选，自然语言订单请求；Tool 可以从中提取明确的长数字订单号。
- `order_id`：可选。有订单号时查询单个订单。
- `limit`：无订单号时返回最近 N 个订单摘要，默认 `5`，范围 `1..20`。
- `user_id`：不属于 public input，由可信 Runtime 注入 internal input，Orchestrator 不得提供。

有 `order_id` 输出：

```json
{
  "result_type": "single_order",
  "order": {
    "id": 202607020001,
    "status": 2,
    "status_label": "paid",
    "items": [],
    "logistics": {}
  },
  "candidates": []
}
```

无 `order_id` 输出：

```json
{
  "result_type": "order_candidates",
  "order": null,
  "candidates": [
    {
      "id": 202607020001,
      "status": 2,
      "status_label": "paid",
      "pay_amount": "197.00",
      "created_at": "2026-07-02T00:00:00",
      "item_count": 1,
      "first_item_name": "Logitech ...",
      "logistic_no": "..."
    }
  ]
}
```

查不到时：

```json
{
  "result_type": "not_found",
  "order": null,
  "candidates": []
}
```

安全边界：

- 指定订单时固定校验 `order_id + user_id`。
- 不允许只按 `order_id` 跨用户查询。
- 不使用 LLM，不走 NL2SQL。
- 不修改订单，不取消订单，不办理售后。

主流程建议：

- 用户只说“查我的订单”时，调用 `order.lookup` 且不传 `order_id`。
- 返回多个候选时，主流程追问用户选择哪一单。
- 用户给出明确订单号后，再带上 `user_id + order_id` 查询详情。

## policy.search

用途：

- 检索售后、退换货、退款、保修、价保、发票、发货、用户隐私与数据访问等政策问题。

数据来源：

- 本地 JSON：`backend/data/knowledge_documents.json`。
- 不依赖 PostgreSQL。
- 不依赖 ChromaDB。
- 不调用 LLM。

默认检索范围：

- `policy`
- `store_rule`
- `faq`

输入：

```json
{
  "query": "退货多久退款",
  "document_type": null,
  "limit": 3
}
```

字段说明：

- `query`：必填，用户政策问题。
- `document_type`：可选，如果主流程明确要限定文档类型，可以传 `policy`, `store_rule`, `faq`。
- `limit`：请求的 chunk Top-K，默认 `3`，范围 `1..10`；Tool 内部最小值为 `2`，传入
  `1` 时仍按 Top-2 检索。
- `retrieval_mode` 不暴露给主流程 LLM，由 Tool 内部固定为 `hybrid`。

输出：

```json
{
  "result_type": "documents",
  "documents": [
    {
      "source_type": "knowledge_document",
      "source_id": 1,
      "title": "PC 外设售后与退换货政策",
      "document_type": "policy",
      "snippet": "...",
      "score": 0.18,
      "metadata": {
        "scenario": "after_sales",
        "retrieval_debug": {
          "bm25_score": 1.23,
          "vector_score": 0.76,
          "rrf_score": 0.18,
          "bm25_rank": 1,
          "vector_rank": 1,
          "chunk_id": "1:0",
          "vector_chunk_id": "1:0"
        }
      }
    }
  ],
  "search_strategy": "hybrid"
}
```

检索模式：

- 主流程固定使用 `hybrid`；以下单路模式只保留给 Tool 内部测试与检索评估。
- `bm25`：只走 BM25。
- `vector`：只走本地向量检索。工具读取 `backend/data/knowledge_vector_index.json` 中的
  chunk embedding，使用 `BAAI/bge-small-zh-v1.5` 对 query 生成向量，并按 cosine
  similarity 对 chunk 排名。
- `hybrid`：BM25 + vector，两路结果在 chunk 级使用 RRF 融合；同一文档可以返回多个
  相关 chunk。
- `bm25` 模式不会初始化或调用 embedding provider；`vector` / `hybrid` 命中向量分块时，返回的
  `snippet` 是完整命中 chunk，不再额外截成 180 字。
- 固定大小切分不会保留仅由 overlap 构成的短尾 chunk，避免返回重复内容或从半句话开始的
  末尾碎片。
- `SentenceTransformer` 模型按模型名做进程级懒加载缓存。新的
  `KnowledgeRetrievalToolService` / provider 实例复用同一个模型对象，不随每个聊天请求重新加载；
  `uvicorn --reload` 重启或多 worker 部署时，每个新进程仍各自加载一次。已存在本地缓存时优先
  `local_files_only`，只有首次环境尚未下载模型时才访问 HF Hub。

边界：

- 只返回政策依据和流程说明。
- 不办理售后。
- 不承诺退款。
- 不判断责任归属。
- 涉及审批、补偿、特殊承诺时，主流程应转人工或后续业务接口。

## knowledge.search

用途：

- 检索品牌、商家、外设知识、选购知识、FAQ。

数据来源：

- 本地 JSON：`backend/data/knowledge_documents.json`。
- 不依赖 PostgreSQL。
- 不依赖 ChromaDB。
- 不调用 LLM。

默认检索范围：

- `brand`
- `peripheral_knowledge`
- `faq`
- `store_rule`

输入：

```json
{
  "query": "Wooting 磁轴键盘适合什么场景",
  "document_type": null,
  "limit": 3
}
```

输出结构：

- 与 `policy.search` 相同。
- `limit` 同样表示 chunk Top-K，Runtime 默认传 `3`，Tool 内部最小为 `2`。
- `search_strategy` 会返回实际检索模式。
- 每条文档 metadata 会包含 `retrieval_debug`，便于调试不同检索模式效果。

边界：

- 只提供知识解释和选购辅助。
- 品牌说明不代表官方授权声明。
- 商品价格、库存、销量、SKU 规格必须以 `catalog.search` / `catalog.compare` 的结构化查询结果为准。

## 本地知识文档

文件：

```text
backend/data/knowledge_documents.json
backend/data/knowledge_vector_index.json
```

当前包含 6 篇：

- `PC 外设售后与退换货政策`，类型 `policy`
- `订单发货、物流、价保与发票规则`，类型 `store_rule`
- `PC 外设商城常见问题 FAQ`，类型 `faq`
- `PC 外设选购知识指南`，类型 `peripheral_knowledge`
- `品牌与商家知识说明`，类型 `brand`
- `用户隐私与数据访问规则`，类型 `policy`

文档格式：

```json
{
  "id": 1,
  "title": "PC 外设售后与退换货政策",
  "document_type": "policy",
  "content": "...",
  "metadata": {}
}
```

注意：

- 本地文档加载使用缓存；修改 JSON 后建议重启后端。
- 修改 `knowledge_documents.json` 后，需要重新构建向量索引。
- 向量索引构建命令：

```bash
cd backend
python -m scripts.build_knowledge_vector_index
```

- 默认 embedding 模型：`BAAI/bge-small-zh-v1.5`。
- 第一次构建索引会下载模型权重；之后会使用本地缓存。
- 当前不使用 Chroma；如果后续文档规模明显增大，再考虑切换到 Chroma 或其他向量库。

## 主流程调用建议

- 商品推荐：优先 `catalog.search`。
- 商品对比：优先 `catalog.compare`。
- 查订单：优先 `order.lookup`。
- 售后政策、退款、退换货、价保、发票、发货：优先 `policy.search`。
- 品牌、商家、外设选购知识、FAQ：优先 `knowledge.search`。
- 如果用户问题同时包含商品和政策，例如“这个鼠标能不能退”，可先用 `catalog.search` 找商品事实，再用 `policy.search` 找政策依据，最终由主流程合成回答。
- 如果用户问题需要执行写操作，例如取消订单、改地址、创建售后、审批退款，当前 tools 不处理，主流程应转人工或调用后续业务接口。

## 已验证

- `catalog.search` 可以连接 PostgreSQL 并返回商品。
- `catalog.search` 可以对无 category 的受控场景使用独立数据库 Session 并行展开，并按品类聚合。
- `order.lookup` 可以连接 PostgreSQL 并返回订单候选。
- `policy.search` / `knowledge.search` 可以检索本地 JSON 和本地向量索引。
- 后端验证命令：

```bash
cd backend
.venv/bin/pytest -q \
  tests/test_catalog_repository.py tests/test_catalog_eval.py tests/test_tools.py \
  tests/test_orchestrator.py tests/test_orchestrator_wave_loop_cases.py \
  tests/test_agent_tool_wiring.py
.venv/bin/ruff check .
```

当前验证结果：

```text
238 passed
All checks passed
```

完整后端当前为 `383 passed, 3 failed`；三个失败均位于未由本次 Catalog 改动触及的长期记忆和
会话历史测试，不能把完整仓库描述为全绿。

## 阶段二商品 Planner 接入状态

`catalog.search` 和 `catalog.compare` 现在都走受控 query plan，而不是让 LLM 直接生成 SQL。

执行链路：

```text
自然语言输入
-> CatalogQueryPlanner
-> ProductQueryPlan / CatalogComparePlan JSON
-> Query Guard 白名单校验
-> Usage Scenario Mapping（如存在受控场景）
-> ProductSearchRequest
-> SQLAlchemy 查询 PostgreSQL
-> 结构化 tool output
```

默认行为：

- 默认启用真实 LLM planner；只要 .env 配置了 LLM_API_KEY，商品 tools 会优先走真实 LLM。
- 如需临时关闭真实 LLM planner，可在 `.env` 设置 `CATALOG_LLM_PLANNER_ENABLED=false`，代码会退回 rule-based planner。
- 启用后，`build_tool_registry(session)` 会自动为商品 tools 注入 `LLMCatalogQueryPlanner`。
- 如果没有 key、显式关闭开关、或 LLM planner 初始化失败，则自动使用 `RuleBasedCatalogQueryPlanner`。

稳定性策略：

- LLM 只允许返回 JSON plan，不允许返回 SQL。
- `ProductQueryPlan` / `CatalogComparePlan` 会校验 category、filter、sort、limit、comparison_fields 等白名单字段。
- `ProductQueryPlan.usage_scenario` 只允许 `office`、`gaming`、`video_meeting`、
  `live_streaming` 或 `null`；LLM 漏识别时会使用确定性 query 规则补全，未知自由值不能进入执行层。
- planner 输出非法 JSON、非法字段、过度约束或异常时，tool fallback 到 rule-based planner。
- fallback 原因会写入 `query_plan.fallback_reason`，主流程可用于调试，但不建议直接展示给用户。

`catalog.compare` 召回策略：

- 如果 compare plan 识别出多个对比对象，例如 `Logitech G502` 和 `Razer Viper`，tool 会按对象分别召回候选。
- 这样可以避免一次整体查询导致结果被单一品牌或单一对象占满。
- 如果主流程已经拿到明确 `sku_ids`，仍优先走 direct SKU 对比，不再做自然语言召回。
## 补充：默认 LLM Planner 行为

- `catalog.search` 和 `catalog.compare` 运行时默认启用真实 `LLMCatalogQueryPlanner`。
- 需要在仓库根目录 `.env` 配置 `LLM_API_KEY`，以及 `LLM_PROVIDER` / `LLM_MODEL`。
- 如果没有 `LLM_API_KEY`，或显式设置 `CATALOG_LLM_PLANNER_ENABLED=false`，会自动回退到 `RuleBasedCatalogQueryPlanner`，主流程调用方式不变。
- LLM 只返回 JSON query plan，不直接生成或执行 SQL；tool 会先做 guard 校验，再用 SQLAlchemy 查 PostgreSQL。

## catalog.facets

用途：

- 查询商品目录元数据和聚合选项，而不是返回商品列表。
- 适合“你们卖哪些品牌的鼠标”“Razer 有哪些外设”“显示器有哪些刷新率”“键盘有哪些轴体”等问题。

LLM-safe name：`catalog_facets`

Registry name：`catalog.facets`

Orchestrator 可见的 public input：

```json
{
  "query": "你们卖哪些品牌的鼠标",
  "limit": 20
}
```

Tool 内部推导的 facet plan 示例，不向 Orchestrator 暴露：

```json
{
  "query": "你们卖哪些品牌的鼠标",
  "facet": "brand",
  "category": "mouse",
  "brand": null,
  "spec_key": null,
  "min_price": null,
  "max_price": null,
  "filters": {},
  "limit": 20
}
```

支持的 `facet`：

- `brand`：返回某类目/条件下有哪些品牌及数量。
- `category`：返回某品牌/条件下有哪些类目及数量。
- `spec_key`：返回某类目/条件下有哪些规格字段。
- `spec_value`：返回某个规格字段有哪些可选值，例如 `switches`、`refresh_rate`、`resolution`。

主编排只选择 `catalog_facets` 并保留完整 `query`，不自行猜测 `facet`、`category`、`brand`
或 `spec_key`。

输出示例：

```json
{
  "result_type": "facets",
  "facet": "brand",
  "items": [
    {"value": "Logitech", "count": 12},
    {"value": "Razer", "count": 8}
  ],
  "category": "mouse",
  "brand": null,
  "spec_key": null,
  "query_plan": {}
}
```

边界：

- 只返回目录元数据和 count，不返回商品详情。
- 如果用户要具体商品推荐，使用 `catalog.search`。
- 如果用户要两个商品事实对比，使用 `catalog.compare`。
- 不走 LLM，不生成 SQL；由 SQLAlchemy 查询 PostgreSQL 后聚合。

## 交付同步信息：Contract / Registry / Handler 收口

- 已将 `ToolContract + handler` 统一到 `BoundTool` / `ToolCatalog`，位置：`backend/app/tools/contracts.py`。
- Provider 和 Executor 都从同一个 `ToolCatalog` 派生：`DefaultToolContractProvider` 导出 contract/schema，`RegistryToolExecutor` 解析并执行 `BoundTool`。
- `llm_name` 和 `registry_name` 继续分属两个命名空间，例如 `catalog_search` 和 `catalog.search`；映射只在 `ToolContract` 中定义一次。
- `ToolCatalog` 构建时会校验 `llm_name` 唯一、`registry_name` 唯一、handler 存在、handler 输入模型匹配、handler 输出模型匹配。
- handler 成功输出会在 executor 边界通过 `contract.output_model` 二次校验；业务空结果、`not_found`、`unsupported_query` 都保持 `ok=true`，不和系统错误混同。
- 依赖异常映射：SQLAlchemy 异常和本地文件/索引 `OSError` 映射为 `dependency_unavailable`；超时映射为 `timeout`；未知异常统一映射为 `execution_error`。
- Tool Contract 级别仍保持 `parallel_safe=False`，主 Orchestrator 不应据此并行发起多个共享请求
  Session 的 PostgreSQL Tool Call。`catalog_search` 在无 category 的受控场景下拥有独立的内部
  并行路径：每个品类任务创建自己的 `AsyncSession`，并发上限为 3。这是 Tool 内部实现细节，
  不改变 Contract 级并行声明。

当前验收命令：

```bash
cd backend
.venv/bin/pytest
.venv/bin/ruff check .
```

当前验收结果：

```text
Targeted Catalog / Tool / Orchestrator: 238 passed
Full backend: 383 passed, 3 known unrelated failures
Ruff: All checks passed
```

## 主流程接入注意事项（2026-07-20 更新）

- 商品类 tool 的公开入参尽量只传用户原始 `query` 和 `limit`；`catalog_search`、`catalog_compare`、`catalog_facets` 内部会做结构化解析、品牌/品类/规格中英文归一化、白名单校验和必要 fallback。
- 主流程不要自己生成 SQL，也不要把中文 key（例如“连接方式”“颜色”）传给商品 tool；如果外部 LLM 传错 schema，应优先只保留 `query` 重新调用。
- `catalog_search` 的业务空结果和不支持查询都会以 `ok=true` 返回，主流程需要读取
  `diagnostics[0].code` 或 `query_plan.error_type` 区分：`empty_result` 表示条件合法但无商品，
  `unsupported_query` 表示当前目录字段不支持，`usage_mapping_unavailable` 表示缺少该场景与
  品类的可靠规格映射，`invalid_catalog_plan` 表示 LLM planner 失败后已使用规则 fallback。
- 白名单是按品类收紧的：例如当前数据库的显示器没有 `connection_type` 字段，所以“蓝牙显示器”会返回 `unsupported_query`，不应被主流程解释成“系统错误”或“库存为 0”。
- `catalog_facets` 用于回答“你们卖哪些品牌/品类/规格选项”，例如“你们卖哪些品牌的鼠标”“键盘有哪些轴体”“音箱功率有哪些档位”；不要用 `catalog_search` 代替这类元数据问题。
- `catalog_compare` 可以直接传自然语言对比 query；如果 LLM planner 把可支持的品牌/品类对比误判为 unsupported，tool 内部会规则 fallback，并通过 `diagnostics` 标出。
- 主流程最终回答里注意销量语义：`sku_sales_count` 是 SKU 级销量，`sales_count` 是 SPU 级聚合销量；比较颜色/版本销量时只能依据 `sku_sales_count`。
- 订单 tool 仍只需要主流程传 `order_id`（有明确订单号时）或 `query`；`user_id` 由 runtime 注入，主流程不要让外部 LLM 提供或覆盖用户身份。

既有真实评测覆盖 5 类 × 5 个口语化中文 case，共 25 个，覆盖推荐、品牌/预算/颜色/连接方式、
facets、compare、empty/unsupported 诊断。本次另外通过正式 Tool Contract 对 PostgreSQL 验证了
办公、游戏、视频会议、直播四个无 category 场景的并行展开、规格约束和返回品类。

当前验收结果：

```text
Scenario mapping + PostgreSQL examples: 4 passed
Targeted Catalog / Tool / Orchestrator tests: 238 passed
Ruff: All checks passed
```
