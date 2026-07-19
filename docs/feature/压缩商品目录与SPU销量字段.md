---
title: 压缩商品目录与 SKU/SPU 销量字段
category: feature
tags: [feature, 商品数据, 数据清理, Alembic, seed, catalog]
---

# 压缩商品目录与 SKU/SPU 销量字段

## 背景

本地商品数据需要从外部大数据集收敛到一个可控演示规模。建表逻辑继续沿用现有
Alembic migration 链，不改已经合入的历史 migration；商品数据导入改为新的本地受控脚本。

## 目标规模

- 类目固定为：鼠标、键盘、耳机、显示器、音箱、摄像头。
- 每个类目 4 个品牌，满足 3-5 个品牌要求。
- 每个品牌 8 个 SPU，满足 8-10 个产品要求。
- 每个 SPU 12 个 SKU，满足 10-20 个 SKU 要求。
- 目标商品总量：6 个类目、24 个类目-品牌组合、192 个 SPU、2304 个 SKU。

## 数据重建方案

- 使用 `./scripts/podman-infra.sh reset` 删除本地 PostgreSQL、Redis、ChromaDB volume。
- 重新执行 `alembic upgrade head` 建表。
- 执行新的 `scripts.import_compact_catalog` 导入 6 类目商品。
- 执行 `scripts.seed_demo` 补 demo 用户、凭据、示例订单、物流和知识文档。
- 执行 `scripts.sync_knowledge` 重新同步 ChromaDB。

`scripts.seed_demo` 保留旧 demo 商品兜底：如果库里没有任何 active SKU，它仍会写入少量 demo 商品；
如果新目录已经导入，它只复用已有 SKU 创建示例订单，避免污染目标商品规模。

## SKU 与 SPU 销量字段

SKU 和 SPU 均使用以下字段：

- 字段名：`sales_count`
- 类型：`INTEGER`
- 默认值：`0`
- 约束：`NOT NULL`，并增加 `sales_count >= 0` check constraint

理由：销量是非负计数，当前系统没有需要小数或超大整数的销量场景；`sales_count` 比 `sales`
更明确，避免把金额销售额和销量计数混在一起。

- `sku.sales_count` 是单个 SKU 的销量，是销量数据的事实来源。
- `spu.sales_count` 是同一 SPU 下全部 SKU 销量之和，保留为目录排序和查询使用的聚合值。
- PostgreSQL trigger 在 SKU 新增、修改、删除或变更所属 SPU 时同步维护总销量。
- 升级已有数据库时，历史 SPU 销量会按 SKU ID 顺序均匀拆分，迁移前后的 SPU 总销量不变。

## 验证

- `cd backend && .venv/bin/pytest tests/test_import_compact_catalog.py tests/test_catalog_repository.py::test_category_aliases_include_compact_catalog_categories -q`
- `cd backend && .venv/bin/pytest && .venv/bin/ruff check .`
- 本地重建数据：`CONFIRM_RESET=1 ./scripts/podman-infra.sh reset && make setup-local`
