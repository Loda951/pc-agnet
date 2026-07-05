# 右侧面板信息架构与视觉层次重设计

> 日期: 2026-07-04
> 状态: Draft
> 阶段: Phase 2 — P1 前端布局优化（续）

## 背景

当前右侧面板（ContextPanel）存在两个核心问题：

1. **缺少视觉层次感** — 面板背景 `--bg-panel` (#131417) 与卡片背景 `--bg-card` (#1e2024) 差距太小（仅 5 个亮度级），1px 边框在暗色下几乎不可见，整个面板看起来像一个黑色平面
2. **信息架构混乱** — 6+ 个区块（指标、边界、接管、上下文、依据、商品、订单、售后）平铺展示，没有主次之分，用户不知道先看什么

## 设计目标

将右侧面板从"信息平铺"改为"分层辅助"——作为对话上下文辅助面板，按优先级组织信息，让用户一眼看出当前对话的关键状态和推荐内容。

## 面板定位

右侧面板是**对话上下文辅助面板**，不是独立信息工作台。它的职责是：
- 告诉用户当前对话的边界状态（自动回答/人工接管/超出范围）
- 展示与当前对话相关的推荐商品
- 提供证据来源和订单详情作为补充信息

## 信息架构重组

### 三层优先级模型

| 层级 | 名称 | 内容 | 视觉权重 | 何时显示 |
|------|------|------|----------|----------|
| **L1 状态区** | Status | 边界状态 + 指标条 | 高对比、醒目颜色徽标 | 始终 |
| **L2 重点区** | Primary | 商品卡片、订单卡片 | 卡片样式、铜色左边框、渐变背景 | 条件显示（有数据时） |
| **L3 详情区** | Details | 依据列表、上下文时间线、售后表单 | 紧凑文字列表、低对比 | 条件显示（有数据时） |

### 区块排序（从上到下）

```
┌─────────────────────┐
│ 📊 指标条           │  L1: SKU · 订单 · 依据
├─────────────────────┤
│ 🛡️ 边界状态        │  L1: 自动回答 / 人工接管 / 超出范围
├─────────────────────┤
│ 📦 商品卡片 ×N      │  L2: 图片+标题+规格+价格（重点区）
│ ┌─────────────────┐ │
│ │ 🖱️ 商品名        │ │
│ │ 品牌 · ¥价格     │ │
│ └─────────────────┘ │
├─────────────────────┤
│ 🚚 订单卡片         │  L2: 订单号+金额+物流（条件显示）
├─────────────────────┤
│ 📋 依据列表         │  L3: 紧凑列表（条件显示）
│ 🔄 上下文时间线     │  L3: 最近5轮摘要
│ 🎧 接管面板         │  条件: human_handoff_required
└─────────────────────┘
```

**关键变化 vs 当前实现：**
- 指标条从面板顶部独立显示改为紧接边界状态下方
- 边界状态从"卡片"改为"状态条"——更紧凑，用背景色区分类型
- 商品区从面板中段移到边界下方，作为重点内容
- 依据和上下文区移到最下方，视觉权重最低

## 视觉层次方案

### 不引入新颜色，改变现有颜色的使用方式

| 层级 | 元素 | 当前 | 改为 |
|------|------|------|------|
| 面板背景 | `.context-panel` | `--bg-panel` (#131417) | `--bg-base` (#0c0d0f) — 更深，让区块更突出 |
| 区块背景 | `.panel-section` | 无背景 | `--bg-panel` (#131417) — 轻微提亮 |
| 区块分隔 | `.panel-section` 之间 | 无分隔 | `border-bottom: 1px solid var(--border-subtle)` |
| 卡片背景 | `.product-card` 等 | `--bg-card` (#1e2024) | 不变 |
| 重点卡片 | 商品卡片 | 无特殊标记 | `border-left: 3px solid var(--copper)` — 铜色左边框条 |
| 标题图标 | `.section-title svg` | `--copper` 色 | 添加 `--copper-weak` 圆形背景 |

### 状态条设计

边界状态从卡片改为紧凑状态条：

```css
.boundary-status-bar {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  font-size: 13px;
  font-weight: 600;
}

.boundary-status-bar.auto {
  background: var(--green-weak);
  color: var(--green);
  border-left: 3px solid var(--green);
}

.boundary-status-bar.handoff {
  background: var(--amber-weak);
  color: var(--amber);
  border-left: 3px solid var(--amber);
}

.boundary-status-bar.blocked {
  background: var(--crimson-weak);
  color: var(--crimson);
  border-left: 3px solid var(--crimson);
}
```

### 商品卡片左边框条

```css
.product-card {
  border-left: 3px solid var(--copper);
  border-radius: var(--radius-md);
  /* 其他样式不变 */
}
```

### 区块标题图标背景

```css
.section-title svg {
  color: var(--copper);
  background: var(--copper-weak);
  padding: 4px;
  border-radius: var(--radius-sm);
}
```

## 文件变更清单

### 修改文件
| 文件 | 改动 |
|------|------|
| `frontend/src/components/ContextPanel.tsx` | 重组区块顺序（指标→边界→商品→订单→依据→上下文→接管）；边界状态改为 `BoundaryStatusBar` 组件 |
| `frontend/src/styles.css` | 面板背景改为 `--bg-base`；`.panel-section` 添加背景和分隔线；商品卡片添加铜色左边框；区块标题图标添加背景；新增 `.boundary-status-bar` 样式；移除 `.boundary-card` 样式 |

### 不修改
| 文件 | 原因 |
|------|------|
| 后端代码 | 无需改动 |
| `frontend/src/types.ts` | 无需改动 |
| `frontend/src/App.tsx` | 无需改动 |

## 技术风险

| 风险 | 缓解 |
|------|------|
| 状态条替代边界卡片可能影响接管面板的展示 | 接管面板（handoff）从边界卡片中拆出，作为独立区块条件显示 |
| 移动端抽屉模式需要同步更新 | CSS 响应式已覆盖，只需确保区块顺序一致 |
| 商品卡片左边框在移动端可能过宽 | 移动端可将 `border-left` 改为 `border-top` 或减小宽度 |