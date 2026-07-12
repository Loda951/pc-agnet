# Tooluse Tools 接入说明

本文档面向主流程编排同学，说明当前分支提供的进程内业务 tools、输入输出、数据来源和使用边界。

## 总体边界

- 本分支只提供 tool 注册、schema 校验、tool 执行逻辑和结构化返回。
- 主流程负责意图识别、选择 tool、调用顺序、结果融合和最终自然语言回答。
- 当前不提供 MCP，不负责 LangGraph 主流程节点改造，不负责 SSE tool_call 事件。
- 商品和订单 tools 读取 PostgreSQL。
- 政策和知识 tools 读取本地 JSON 文档，并使用 BM25 / vector / hybrid 检索。
- 向量检索使用本地真实 embedding 模型 `BAAI/bge-small-zh-v1.5`，通过 `sentence-transformers` 在本机生成向量。
- 向量索引持久化在本地 JSON 文件中，不写 PostgreSQL，不写 Chroma，不依赖外部 embedding API key。

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

输入：

```json
{
  "query": "Logitech wireless mouse under 300",
  "category": "mouse",
  "brand": "Logitech",
  "min_price": null,
  "max_price": 300,
  "filters": {
    "wireless": "wireless"
  },
  "limit": 3
}
```

字段说明：

- `query`：必填，自然语言需求。
- `category`：可选，类目过滤。
- `brand`：可选，品牌过滤。当前 planner 会识别常见品牌；repository 层主要通过 query token 命中品牌。
- `min_price` / `max_price`：可选，价格范围。
- `filters`：可选，规格过滤，例如 `wireless`, `connection_type`。
- `limit`：默认 `3`，范围 `1..20`。

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
    "sort": "recommend",
    "limit": 3,
    "supported": true,
    "unsupported_reason": null,
    "planner": "rule_based",
    "fallback_reason": null
  }
}
```

排序策略：

- 匹配分优先。
- 同分时按 `sales_count` 降序。
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

输入：

```json
{
  "user_id": 1,
  "order_id": 202607020001,
  "limit": 5
}
```

字段说明：

- `user_id`：必填，必须由主流程从登录态或可信上下文传入。
- `order_id`：可选。有订单号时查询单个订单。
- `limit`：无订单号时返回最近 N 个订单摘要，默认 `5`，范围 `1..20`。

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

- 检索售后、退换货、退款、保修、价保、发票、发货等政策问题。

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
  "limit": 3,
  "retrieval_mode": "hybrid"
}
```

字段说明：

- `query`：必填，用户政策问题。
- `document_type`：可选，如果主流程明确要限定文档类型，可以传 `policy`, `store_rule`, `faq`。
- `limit`：默认 `3`，范围 `1..10`。
- `retrieval_mode`：默认 `hybrid`，可选 `bm25`, `vector`, `hybrid`。

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
          "vector_chunk_id": "1:0"
        }
      }
    }
  ],
  "search_strategy": "hybrid"
}
```

检索模式：

- `bm25`：只走 BM25。
- `vector`：只走本地向量检索。工具读取 `backend/data/knowledge_vector_index.json` 中的 chunk embedding，使用 `BAAI/bge-small-zh-v1.5` 对 query 生成向量，并按 cosine similarity 聚合到文档级结果。
- `hybrid`：BM25 + vector，两路结果用 RRF 融合。

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
  "limit": 3,
  "retrieval_mode": "hybrid"
}
```

输出结构：

- 与 `policy.search` 相同。
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

当前包含 5 篇：

- `PC 外设售后与退换货政策`，类型 `policy`
- `订单发货、物流、价保与发票规则`，类型 `store_rule`
- `PC 外设商城常见问题 FAQ`，类型 `faq`
- `PC 外设选购知识指南`，类型 `peripheral_knowledge`
- `品牌与商家知识说明`，类型 `brand`

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
- `order.lookup` 可以连接 PostgreSQL 并返回订单候选。
- `policy.search` / `knowledge.search` 可以检索本地 JSON 和本地向量索引。
- 后端验证命令：

```bash
cd backend
.venv/bin/pytest tests/test_tools.py
.venv/bin/ruff check app/tools tests/test_tools.py app/repositories/catalog.py app/repositories/orders.py app/schemas/catalog.py
```

当前验证结果：

```text
8 passed
All checks passed
```

## 阶段二商品 Planner 接入状态

`catalog.search` 和 `catalog.compare` 现在都走受控 query plan，而不是让 LLM 直接生成 SQL。

执行链路：

```text
自然语言输入
-> CatalogQueryPlanner
-> ProductQueryPlan / CatalogComparePlan JSON
-> Query Guard 白名单校验
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

输入示例：

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
- 当前没有 tool 验证为可并行执行，全部保持 `parallel_safe=False`；原因是 PostgreSQL tools 共享当前 SQLAlchemy `AsyncSession`。

当前验收命令：

```bash
cd backend
.venv/bin/pytest
.venv/bin/ruff check .
```

当前验收结果：

```text
109 passed
All checks passed
```
