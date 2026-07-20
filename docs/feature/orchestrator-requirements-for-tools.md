# 主编排对 Tool 的当前修正需求

## 1. 文档状态与目的

本文只记录当前尚未完成的一项 Tool 修正需求。已经完成的 Tool Contract、`BoundTool` /
`ToolCatalog`、public/internal input 分离、Runtime 身份注入、稳定错误分类和 output 校验等旧需求
已删除，不再作为待办重复记录。

相关实现：

- Catalog Tool：`backend/app/tools/catalog.py`
- Catalog Repository：`backend/app/repositories/catalog.py`
- Tool schema：`backend/app/tools/schemas.py`
- Tool Contract：`backend/app/tools/contracts.py`

## 2. Catalog 用途语义必须与真实查询能力一致

### 2.1 当前问题

当前 Catalog Planner 可以生成：

```json
{
  "usage_scenario": "office",
  "supported": true
}
```

但是 `ProductQueryPlan -> ProductSearchRequest` 只下沉了 `excluded_usage`，没有把正向
`usage_scenario` 转成 SQL 条件、规格过滤或召回后过滤。Repository 也只实现了
`excluded_usage` 的文本排除逻辑。

本地商品库目前也没有可直接查询的用途数据：

- 商品标题和 `specs_json` 中包含 `office` 或“办公”的 SKU 数量为 0。
- EAV 属性中不存在 `usage`、`用途`、`office` 或“办公”相关键值。
- 现有数据主要是品类、品牌、价格、库存、轴体、连接方式和尺寸等具体规格。

因此，正向用途查询目前并不是真实可执行的 Catalog 能力。现在的问题不是 Orchestrator 少传了
结构化参数，而是 Tool 将没有实际执行的用途条件标记为了 `supported=true`。

另外，`excluded_usage` 只是基于标题、品类和规格文本进行排除的弱启发式，不能等同于正式的
用途标签查询。这也解释了为什么当前实现里只有 `excluded_usage`：它可以在缺少用途字段时做文本
排除，而正向用途无法据此证明商品真的适合该用途。

### 2.2 修正要求

推荐 Tool 侧组合实现以下两层能力：

1. **正式用途标签作为主要能力**：为商品补充受控的 `usage_scenario` / tags 数据，让正向用途
   和排除用途都优先通过同一套字段执行。用途值需要使用固定枚举和统一命名，避免由 LLM 自由
   生成不可查询的标签。
2. **确定性规格映射作为兜底能力**：当商品尚未补齐用途标签时，由 Tool 把“办公”等用途映射为
   可查询、可测试的具体规格，并返回实际应用的条件。映射规则应由代码或配置维护，不能每次由
   LLM 临时猜测。输出中必须标明结果属于规则推断，而不是用途标签匹配。

   例如，用户请求“推荐办公键盘”，而商品还没有 `usage_scenario=office` 标签时，可以在 Tool
   配置中维护一条固定规则：

   ```yaml
   office_keyboard:
     category: keyboard
     prefer:
       connection: [bluetooth, wireless]
       switch_type: [silent_red, red, brown]
     exclude:
       switch_type: [blue, clicky]
   ```

   Tool 将这条规则转换为 Repository 真正支持的规格过滤或排序条件，例如：先限定键盘品类，
   排除青轴或其他 clicky 轴体，再优先返回支持蓝牙/无线、静音红轴/红轴/茶轴的商品。这里的字段
   名称和允许值只是示例，正式规则必须根据数据库已有规格字段和值确定。

   返回结果需要说明实际使用了哪些条件，例如：

   ```json
   {
     "supported": true,
     "match_type": "spec_rule_inference",
     "usage_scenario": "office",
     "applied_conditions": {
       "category": "keyboard",
       "preferred_connection": ["bluetooth", "wireless"],
       "preferred_switch_type": ["silent_red", "red", "brown"],
       "excluded_switch_type": ["blue", "clicky"]
     }
   }
   ```

   这样 Orchestrator 可以准确说明“这些商品是根据办公场景偏好的连接方式和轴体筛选出来的”，
   而不是声称数据库把它们标记成了“办公键盘”。

建议执行顺序为：先查询正式用途标签；没有足够的标签数据时，再使用确定性规格映射；两者都
无法执行时返回 `unsupported_query`。不要退化为忽略用途条件后查询普通商品。

实现必须满足：

- `supported=true` 只能表示用途条件确实进入了查询、过滤或有明确规则的排序逻辑。
- Planner 生成但执行层忽略的字段不能被报告为已应用。
- Tool Result 必须让 Orchestrator 区分“真实用途标签匹配”“规格规则推断”和“当前不支持”。
- 正向 `usage_scenario` 与 `excluded_usage` 应优先使用同一套正式用途标签；使用规格映射兜底时，
  需要分别定义包含和排除规则。

### 2.3 Bug 例子：办公键盘推荐

对话：

```text
用户：有什么牌子的键盘
助手：返回 Akko、Keychron、Razer、Wooting 等品牌
用户：用途为办公推荐几个看看
```

实际第一轮 Tool 调用：

```text
catalog_search(
  query="办公键盘 推荐 适合办公的键盘",
  subquery="推荐适合办公用途的键盘商品"
)

query_plan.usage_scenario = "office"
query_plan.supported = true
```

Tool 最终返回了普通键盘商品，但 `office` 没有进入 Repository 的实际查询条件；数据库中也没有
“办公”用途标签或对应文本数据。因此，这批结果不能证明商品适合办公，`supported=true` 与真实
执行能力不一致。

这个例子的关键问题是：

```text
用户表达办公用途
  -> Planner 识别 usage_scenario=office
  -> Tool 声明 supported=true
  -> 执行层忽略 usage_scenario
  -> 返回与办公用途没有可验证关联的普通商品
```

预期行为按以下优先级执行：

- 有正式用途标签时，按标签查询并返回匹配商品。
- 有明确规格映射时，按映射后的具体规格查询，并说明使用了规则推断。
- 两者都没有时，返回 `unsupported_query`，不要把普通商品包装成办公推荐。

### 2.4 最低验收标准

- “用途为办公推荐几个看看”不会在没有查询依据时返回 `supported=true` 的普通商品列表。
- “办公键盘”“游戏鼠标”等正向用途请求明确落入：用途标签匹配、确定性规格推断或
  `unsupported_query` 三者之一。
- `usage_scenario` 一旦被标记为已应用，测试必须证明它改变了 Repository 查询、过滤或排序。
- `excluded_usage` 有独立测试，并明确它目前只是文本排除还是正式用途标签过滤。
- 增加覆盖上述办公键盘问题的回归测试，且不依赖真实 LLM API key。

## 3. Tool 交付时需要同步的信息

请在 PR 或 Tool 文档中说明：

1. 正式用途标签的数据结构、允许值和补充方式。
2. 正向用途与 `excluded_usage` 分别在哪一层执行，如何验证实际生效。
3. 确定性规格映射的规则、配置位置及结果标识方式。
4. 新增的回归测试和本地验收结果。
