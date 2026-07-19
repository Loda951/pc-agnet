# Tooluse Catalog Spec Audit

本工具用于从 PostgreSQL 统计商品规格真实值，辅助维护 `DB_VALUE_ALIASES`，避免凭感觉补中英文别名。

## 运行方式

在 WSL 项目根目录运行：

```bash
cd /home/lyf/projects/pc-agnet/backend
python -m scripts.audit_catalog_spec_values   --output backend/data/catalog_spec_values_audit.json   --markdown-output docs/feature/catalog-spec-values-audit.md
```

如果只想看前 N 个高频值：

```bash
python -m scripts.audit_catalog_spec_values --top-values 20
```

## 输出内容

JSON/Markdown 会按 `spec_key` 汇总：

- `value`: 数据库里的真实规格值。
- `sku_count`: 命中的 SKU 数量。
- `categories`: 出现过的类目。
- `brands`: 出现过的品牌。
- `sources`: 来源，可能是 `sku.specs_json` 或 `attribute_value`。

## 使用原则

- 只把高置信、语义明确的值补进 `DB_VALUE_ALIASES`。
- 不要把模糊词放进 aliases，例如 `好用`、`高端`、`快`。
- plan 层继续使用 canonical value；数据库匹配层负责兼容中英文真实值。
- 数据库更新后重新跑 audit，再补测试和 aliases。
