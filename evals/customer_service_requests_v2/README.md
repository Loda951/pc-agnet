# Customer Service Requests V2 Eval Set

本目录是智能客服项目的新评测集，按“能做什么”和“不能做什么”组织，用于评估 request router、tool planner、工具调用、上下文承接和安全边界。

## 目录结构

- `01_can_do/`：能力展示，包括 direct response、tooluse、multi tool 和 context memory。
- `02_cannot_do/`：可靠性边界，包括 clarification、human handoff、unsupported、out of scope、security refusal。
- `03_mixed_or_edge/`：部分可答、多意图、鲁棒性和注入边界。

## CSV 字段

- `case_id`：稳定用例 ID。
- `level1` / `level2` / `level3`：三级分类，其中 tooluse 下保留具体工具子目录。
- `user_query`：模拟真实用户中文提问，可包含品牌名、型号、规格英文。
- `conversation_context`：评测时可注入的会话上下文；订单号和用户 ID 使用占位符。
- `expected_disposition`：期望 router disposition。
- `expected_tools`：期望工具，多个工具用分号分隔。
- `expected_terminal_type`：期望终态类型。
- `expected_key_assertions`：必须满足的关键断言。
- `expected_forbidden_behavior`：明确禁止的误行为。

## 数据约定

- 商品类 can_do 用例基于 demo PostgreSQL 的品类、品牌和规格：鼠标、键盘、耳机、显示器、音箱、摄像头。
- 订单类用例使用当前 fake demo PostgreSQL 中的实际数字：`user_id=1`、`order_id=202607020001`。
- `999999999999` 是专门用于 `order_not_found` 的不存在订单号。
- 当前数据库暂时只有 `user_id=1` 的订单；用户隔离用例先保留 `<PENDING_OTHER_USER_ID>` 和 `<PENDING_OTHER_USER_ORDER_ID>`，待数据库补第二个 fake 用户/订单后再替换成真实数字。
- SKU/SPU 直接对比类用例使用当前 fake demo PostgreSQL 中的实际数字：`sku_id=1`、`sku_id=2`、`spu_id=1`、`spu_id=2`。


## 评分建议

- Router：检查 `expected_disposition`。
- Tool planner：检查 `expected_tools`、工具入参 schema、是否进入正确工具。
- Tool result：检查空结果、错误分类、SKU/SPU 销量语义、user_id 隔离。
- Final answer：检查 `expected_key_assertions` 和 `expected_forbidden_behavior`。


## 待补数据的用例

- 用户隔离代码能力已支持：`order.lookup` 按 `current_user_id + order_id` 查询。当前数据库只有 `user_id=1` 的订单，所以 `01_can_do/tooluse/order_lookup/user_isolation.csv` 先保留 `<PENDING_OTHER_USER_ID>` 和 `<PENDING_OTHER_USER_ORDER_ID>`；明天数据库补第二个 fake 用户/订单后替换成真实数字即可执行。
