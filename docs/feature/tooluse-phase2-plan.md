# Tooluse 阶段二计划

阶段二目标：增强商品结构化查询能力，让 `catalog.search` / `catalog.compare` 从当前规则解析升级为可接 LLM 的受控查询计划模式。

## 任务清单

1. 定义 `ProductQueryPlan`
   - 字段包括 `category`, `brands`, `min_price`, `max_price`, `filters`, `keywords`, `sort`, `limit`, `supported`, `unsupported_reason`。
   - 作为自然语言和 SQLAlchemy 查询之间的中间层。

2. 实现 Catalog Planner 接口
   - LLM planner 输入自然语言，输出 `ProductQueryPlan`。
   - 不让 LLM 直接生成 SQL。
   - 测试使用 fake planner，不依赖真实 LLM key。

3. 实现 Query Guard
   - 校验 category、brand、price、filters、sort、limit。
   - 非法字段或超出能力范围时 fallback 或返回 unsupported。

4. 增强 `catalog.search`
   - 优先使用 planner 生成 query plan。
   - planner 失败时 fallback 到当前 rule-based planner。
   - 由 Python / SQLAlchemy 根据 query plan 查询 PostgreSQL。
   - 返回 `query_plan` 方便主流程调试。

5. 增强 `catalog.compare`
   - 识别对比对象和对比维度。
   - 复用商品查询和 query plan。
   - 输出对比字段、缺失字段和候选商品事实。

6. Unsupported Query 处理
   - 对超出商品表能力的问题返回 unsupported 和原因。
   - 例如时间维度销量增长、全站销售额统计、用户购买偏好统计等。

7. 商品字段和规格白名单
   - 允许商品域表：`sku`, `spu`, `brand`, `category`, `goods_attribute_relation`, `attribute_key`, `attribute_value`。
   - 允许 filter：无线/有线、DPI、轴体、刷新率、分辨率、麦克风、颜色等。

8. 测试
   - 覆盖预算、品牌、类目、无线/有线、规格过滤、top3、空结果、fallback、unsupported。
   - 覆盖 compare 多商品识别、缺失字段、指定 `sku_ids`。
   - 覆盖 Query Guard 非法字段、非法 sort、超大 limit。

9. 更新主流程说明
   - 更新 `docs/feature/tooluse-tools-for-orchestrator.md`，说明 query plan 输出和 fallback 行为。

10. 可选：商品中文编码问题
   - 当前商品中文字段在部分终端输出中有 mojibake。
   - 如中文查询效果重要，后续修数据导入编码或重导数据。
