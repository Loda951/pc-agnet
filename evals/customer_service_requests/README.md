# PC 外设商城客服请求测试集

本目录提供 5 份可直接导入 Google Sheets 的 UTF-8 CSV，用于分别评测当前 PC 外设商城客服 Agent 的请求分类、工具选择、安全边界和回复策略。

## 文件

- `C1_COMPLETABLE.csv`：业务范围内且系统可完成，40 条。
- `C2_UNSUPPORTED.csv`：业务范围内但系统不支持该功能，15 条。
- `C3_CLARIFY.csv`：业务范围内但需要澄清，15 条。
- `C4_HANDOFF.csv`：业务范围内但需要转人工，15 条。
- `C5_OUT_OF_SCOPE.csv`：不在业务范围内，15 条。

## 类别分布

| code | 类别 | 数量 |
| --- | --- | ---: |
| `C1_COMPLETABLE` | 业务范围内且系统可完成 | 40 |
| `C2_UNSUPPORTED` | 业务范围内但系统不支持该功能 | 15 |
| `C3_CLARIFY` | 业务范围内但需要澄清 | 15 |
| `C4_HANDOFF` | 业务范围内但需要转人工 | 15 |
| `C5_OUT_OF_SCOPE` | 不在业务范围内 | 15 |

`C1_COMPLETABLE` 已进一步覆盖商品推荐、商品对比、目录聚合、商品事实、订单与物流、政策、外设知识、购买流程、身份能力和混合意图。

## 标签说明

- `case_id`：测试样例唯一编号，按类型独立编号，例如 `PC-CS-C1-001`、`PC-CS-C2-001`；每个类型从 `001` 开始。
- `request_class_code`：本测试集的五类业务标签。
- `feature`：请求覆盖的细分功能或场景。
- `user_query`：发送给客服 Agent 的用户请求。
- `conversation_context`：多轮样例的前置上下文或测试前提。空值代表单轮新会话。
- `expected_boundary`：对应系统现有三态边界：`in_scope_auto`、`human_handoff_required`、`out_of_scope`。
- `expected_terminal_type`：期望最终决策类型。需要业务事实的请求在工具执行后应以 `grounded_response` 结束。

当前代码没有独立的 `unsupported_capability` terminal type，因此 `C2_UNSUPPORTED` 的期望策略标为 `direct_response`：明确说明能力限制并给出替代路径。这一列能用于评估后续是否需要新增专门终态。

## Google Sheets 导入

在 Google Sheets 中选择“文件 → 导入 → 上传”，按需上传对应分类的 CSV，分隔符选择“自动检测”或“逗号”，字符集使用 UTF-8。首行是字段名，建议导入后冻结第一行并开启筛选器。

## 设计依据

测试集依据当前仓库的 `README.md`、`docs/codex-context-主线.md`、`backend/app/agent/prompts.py`、`backend/app/agent/intent.py`、`backend/app/tools/contracts.py` 和 `backend/data/knowledge_documents.json` 生成。它描述的是期望行为，部分样例会有意暴露当前规则分类器、澄清机制和不支持能力处理的缺口。
