# Catalog Compare SPU 与 SKU 双层对比

## 背景

原 `catalog_compare` 只返回 SKU 行。两个型号或系列对比时，即使上游按 SPU 销量选中了目标，
后续仍会挑一个代表 SKU 比较，无法可靠回答“这个系列有哪些连接方式、颜色、轴体和价格范围”。

本功能在同一个只读 Tool 中保留 SKU 对比，并新增 SPU 聚合对比，不修改数据库结构。

## 两层语义

- `comparison_level=sku`：比较明确颜色、轴体、连接版本或 SKU；输入使用 `sku_ids`，输出继续使用
  `products`，保持原契约兼容。
- `comparison_level=spu`：比较型号或商品系列；输入使用 `spu_ids`，Tool 查询每个 SPU 下全部
  active SKU，输出使用 `series` 和 `series_differences`。

两种 ID 不能混用。SPU 模式需要至少两个已经解析的 SPU；Tool 不从自然语言猜测系列 ID。

## SPU 聚合规则

每个 series 包含：

- 系列身份、SPU 累计销量；
- `sku_count`、`in_stock_sku_count`、`total_stock`；
- `min_price` 与 `max_price`；
- `common_specs`：每个 active SKU 都存在且值完全相同的规格；
- `option_specs`：其余规格的真实值集合、每个值覆盖的 SKU 数和有货 SKU 数，以及缺失该字段的
  SKU 数；
- `variants`：每个 active SKU 的真实规格组合、价格、库存和 SKU 销量。

`series_differences` 对规格值做集合差异，明确 shared、left-only、right-only 和字段缺失数量。
连接方式等枚举值保持目录原值，不把 Bluetooth、2.4G 和 Wired 粗略压成同一个 Wireless 标签。

## Orchestrator 约束

Router 在 `catalog_compare` Task 上声明 `comparison_level`：

- 型号、系列、SPU 比较使用 `spu`；
- 明确版本、颜色、轴体、连接版本或 SKU 比较使用 `sku`。

Artifact Store 同时保存 `selected_sku_ids` 和 `selected_spu_ids`。Runtime 根据比较层级从
`context_product`、`comparison_context` 或上游 `task_output` 绑定对应 ID。Answer Synthesizer
在 SPU 模式必须以 series 聚合为主证据，不能用单个代表 SKU 概括系列，也不能把多个可选字段
自由组合成数据库中不存在的版本。

## 验证范围

- 旧 SKU 对比输入输出保持兼容；
- SPU 聚合覆盖共同规格、可选规格、价格区间、库存覆盖和真实变体组合；
- Tool outcome 在至少两个 series 时判定 usable；
- “当前系列 + SPU 销量第二”Task DAG 绑定两个 `spu_id`，不绑定代表 `sku_id`；
- working memory 保存比较层级及 SPU ID，支持继续比较同一组系列。
