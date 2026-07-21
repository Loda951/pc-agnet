# 主编排对 Tool 的当前修正需求

## 2026-07-21 当前实现决定

Catalog 当前使用受控场景和版本化规格映射，不新增数据库用途标签或 migration：

- LLM Planner 与 Rule-based Planner 统一识别 `office`、`gaming`、`video_meeting`、
  `live_streaming`，其他自由场景值不能直接进入执行层。
- 映射键为 `usage_scenario + category`，规则版本为 `v1`；只配置现有数据库规格能够支撑的
  品类组合。
- 规则可以包含 `required` 和 `preferred` 条件，并支持 `exact`、`eq`、`in`、`gte`、`lte`。
  `required` 用于过滤，`preferred` 用于有界候选集内的排序加分。
- 已应用规格映射时，不再要求商品标题、类目或规格文本必须包含“办公”“游戏”等用途词；Tool
  Result 通过 `query_plan.usage_mapping` 标明 `deterministic_spec_mapping`、规则版本和实际条件。
- 未配置的场景与品类组合返回 `usage_mapping_unavailable`，不能忽略场景后返回普通商品。
- 未指定品类时，Tool 将场景展开到所有已配置品类，使用独立 `AsyncSession` 以最大并发 3 执行，
  再按品类轮询聚合；结果标记为 `usage_mapping.status=expanded`。
- 正向用途文本匹配仍保留为未使用规格映射时的底层能力；正式数据库用途标签仍属于后续可选演进。

例如“办公键盘”现在通过 `office + keyboard` 的 `静音红轴` 偏好进行排序，并返回规格推断依据，
不再因为数据库没有“办公”文字而固定返回空结果。

## 1. 文档状态与目的

本文记录 Catalog 正向用途链路的原问题、当前实现和主 Orchestrator 可依赖的结果语义。正式用途
标签尚未进入数据库；当前交付的是受控 Planner 场景和确定性规格映射。

相关实现：

- Catalog Tool：`backend/app/tools/catalog.py`
- Catalog Repository：`backend/app/repositories/catalog.py`
- Product search schema：`backend/app/schemas/catalog.py`
- Tool input/output schema：`backend/app/tools/schemas.py`
- Tool Contract：`backend/app/tools/contracts.py`

## 2. Catalog 用途语义必须与真实查询能力一致

### 2.1 原问题（已修复）

Catalog Planner 可以生成：

```json
{
  "usage_scenario": "office",
  "supported": true
}
```

原实现只下沉了 `excluded_usage`，没有把正向 `usage_scenario` 转成过滤或排序条件，导致
`supported=true` 与真实执行能力不一致。

本地商品库目前也没有可直接查询的用途数据：

- 商品标题和 `specs_json` 中包含 `office` 或“办公”的 SKU 数量为 0。
- EAV 属性中不存在 `usage`、`用途`、`office` 或“办公”相关键值。
- 现有数据主要是品类、品牌、价格、库存、轴体、连接方式和尺寸等具体规格。

修复前，正向用途并不是真实可执行的 Catalog 能力；问题位于 Tool 内部，不是主 Orchestrator
少传了结构化参数。

### 2.2 当前实现

当前实现以下能力：

1. LLM Planner 和 Rule-based Planner 只生成 `office`、`gaming`、`video_meeting`、
   `live_streaming` 或 `null`；LLM 漏识别时还会使用同一套确定性 query 规则补全。
2. Tool 使用 `USAGE_SCENARIO_RULES` 按 `scenario + category` 选择 `v1` 规则，输出
   `required` 和 `preferred` 条件。
3. Repository 在分页候选上执行 `required`，并将 `preferred` 加入排序分数。规则支持精确值、
   集合和数值上下界。
4. 已应用规则时，内部 `ProductSearchRequest.usage_scenario` 置空，避免再次要求标题出现用途词。
5. 未配置组合返回 `usage_mapping_unavailable`；Tool 不会忽略用途后返回普通商品。
6. category 为空时，按 `USAGE_SCENARIO_CATEGORIES` 展开；生产路径使用独立数据库 Session
   并行查询，最多同时执行 3 个品类，最终 round-robin 合并并服从原始 `limit`。

当前规则覆盖：

- `office`：键盘、显示器、耳机、摄像头；
- `gaming`：鼠标、键盘、耳机、显示器、音箱；
- `video_meeting`：摄像头、耳机；
- `live_streaming`：摄像头。

例如“办公键盘”应用：

```json
{
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

`excluded_usage` 目前仍是标题、类目和规格文本排除，不等同于正式用途标签。

### 2.3 Bug 例子：办公键盘推荐

对话：

```text
用户：有什么牌子的键盘
助手：返回 Akko、Keychron、Razer、Wooting 等品牌
用户：用途为办公推荐几个看看
```

原问题中的 Tool 调用：

```text
catalog_search(
  query="办公键盘 推荐 适合办公的键盘",
  subquery="推荐适合办公用途的键盘商品"
)

query_plan.usage_scenario = "office"
query_plan.supported = true
```

原错误链路是：

```text
用户表达办公用途
  -> Planner 识别 usage_scenario=office
  -> Tool 声明 supported=true
  -> 执行层忽略 usage_scenario
  -> 返回与办公用途没有可验证关联的普通商品
```

当前链路会识别 `office + keyboard`，应用静音红轴偏好，在候选集中按映射加分，并通过
`query_plan.usage_mapping` 向 Orchestrator 说明推断依据。当前数据库实测返回的前三个商品均为
`switches=静音红轴`。

### 2.4 最低验收标准

- “用途为办公推荐几个看看”不会在没有查询依据时返回 `supported=true` 的普通商品列表。
- “办公键盘”“游戏鼠标”等正向用途请求明确落入确定性规格推断或
  `usage_mapping_unavailable`。
- `usage_scenario` 一旦被标记为已应用，测试必须证明它改变了 Repository 查询、过滤或排序。
- `excluded_usage` 有独立测试，并明确它目前只是文本排除还是正式用途标签过滤。
- 增加覆盖上述办公键盘问题的回归测试，且不依赖真实 LLM API key。

## 3. Tool 交付时需要同步的信息

后续涉及正式用途标签或规则版本升级时，需要同步说明：

1. 正式用途标签的数据结构、允许值和补充方式。
2. 正向用途与 `excluded_usage` 分别在哪一层执行，如何验证实际生效。
3. 确定性规格映射的规则、配置位置及结果标识方式。
4. 新增的回归测试和本地验收结果。
