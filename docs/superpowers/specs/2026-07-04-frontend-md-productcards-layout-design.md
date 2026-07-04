# 前端 MD 渲染、产品卡片与布局优化设计

> 日期: 2026-07-04
> 状态: Draft
> 阶段: Phase 2 — P1 前端布局优化

## 背景

当前前端聊天气泡以纯文本 `<p>` 渲染 assistant 回复，缺少 Markdown 格式化；商品推荐虽然在右侧 ContextPanel 有产品卡片，但聊天区域内没有可视化产品展示；整体布局在小屏设备上体验不佳。

## 目标

1. 助手回复支持完整 Markdown 渲染（加粗、列表、表格、代码块、链接等）
2. 聊天气泡内渲染产品卡片（图片/占位符 + 规格 + 价格）
3. 优化整体三栏布局、右侧面板、侧边栏和移动端适配

## 方案选择

| 方案 | 描述 | 优点 | 缺点 |
|------|------|------|------|
| **A: 轻量 MD + 产品内联卡片** ✅ | 前端加 react-markdown + 产品卡片组件，后端几乎不改，复用 SSE products 数据 | 最小改动，复用已有数据管道 | 产品匹配需要前端逻辑 |
| B: Agent 回复带图片 URL | 让 LLM 输出 Markdown 图片语法 | 最简单 | LLM 可能幻觉 URL；image_url 目前为 null |
| C: 结构化回复 | 后端返回结构化 JSON，前端按段类型渲染 | 最灵活 | 大改后端 Agent 格式，与 delta 流式冲突 |

**选择方案 A**，复用 SSE 已有 `products` 数据管道，图片占位符兜底。

---

## 设计细节

### 1. Markdown 渲染（聊天区）

**组件**: `<MarkdownContent>`

**依赖**:
- `react-markdown` — MD 解析渲染
- `remark-gfm` — GitHub Flavored Markdown 支持（表格、删除线、自动链接）

**渲染器定制**（匹配 PCB 暗色设计系统）:

| 元素 | 样式 |
|------|------|
| `<h1>`~`<h4>` | `--text-primary`，字重 700/600，适当 margin |
| `<strong>` | `--text-primary`，字重 700 |
| `<em>` | 斜体，`--text-secondary` |
| `<a>` | `--copper` 色，hover 下划线 |
| `<ul>/<ol>` | 左缩进，项目符号/数字，间距 |
| `<table>` | 暗色条纹背景，`--border-default` 边框，适合产品参数对比 |
| `<code>` inline | `--font-mono`，`--bg-input` 背景，`--radius-sm` 圆角 |
| `<pre><code>` | 代码块，`--bg-card` 背景，`--radius-md` 圆角，横向滚动 |
| `<blockquote>` | 左边框 `--copper-dim`，`--copper-weak` 背景 |

**流式输出**: `react-markdown` 天然支持内容增量更新，每收到 delta 后 content 变长，组件重新 parse 并渲染。

**用户消息**: 不使用 MD 渲染，保持纯文本 `<p>` + `white-space: pre-wrap`。

**修改位置**:
- `ChatPanel.tsx` — `MessageRow` 组件中 `<p>{bubbleContent}</p>` → `<MarkdownContent>{bubbleContent}</MarkdownContent>`
- 新建 `components/MarkdownContent.tsx`
- 新建 `components/markdown.css` — MD 渲染专属样式

### 2. 聊天内产品卡片

**组件**: `<ProductInlineCard>` + `<ProductCardRow>`

**渲染位置**: assistant 消息的 MD 内容下方，与 MD 文本同在 `bubble-stack` 容器内。

**数据流**:
- SSE `context` 事件推送 `products: ProductCard[]`
- `ChatMessage` 类型扩展 `products?: ProductCard[]` 字段
- App.tsx 在处理 `context` 事件时，将 `products` 关联到当前 streaming 消息（即 `messages` 数组中 `status === "streaming"` 的那条 assistant 消息）
- 当收到 `done` 事件时，`products` 已经存在于该消息对象上，随最终状态一起持久化
- `MessageRow` 检查 `message.products`，有则渲染 `<ProductCardRow>`

**卡片布局**:
- 水平滚动行（`overflow-x: auto`, `scroll-snap-type: x mandatory`）
- 每个卡片约 220px 宽，固定高度
- 卡片内容: 图片/占位符(56x56) + 标题 + 品牌·类别 + 价格·库存

**图片占位符策略**:
- 有 `image_url` → 渲染 `<img>` (懒加载)
- 无 `image_url` → 根据产品 `category` 推断类别，渲染渐变背景 + lucide icon:
  - 鼠标 → `<Mouse />` icon, `--copper-glow` 渐变
  - 键盘 → `<Keyboard />` icon, `--green-glow` 渐变
  - 耳机 → `<Headphones />` icon, `--blue-glow` 渐变
  - 其他 → `<Package />` icon, `--amber-glow` 渐变

**交互**: 点击卡片 → 滚动到右侧 ContextPanel 对应产品并高亮闪烁。

**修改位置**:
- `types.ts` — `ChatMessage` 添加 `products?: ProductCard[]`
- `App.tsx` — SSE context 事件处理时关联 products 到当前消息
- `ChatPanel.tsx` — `MessageRow` 添加产品卡片渲染
- 新建 `components/ProductInlineCard.tsx`
- `styles.css` — 新增 `.product-inline-card`, `.product-card-row` 样式

### 3. 右侧面板优化

**产品卡片** (`ContextPanel` 内的 `ProductCardView`):
- 缩略图从 40px → 56px
- 无图时使用与聊天内产品卡片相同的类别 icon + 渐变占位符
- hover 时展开显示完整规格列表
- grid 布局从 `40px minmax(0, 1fr)` → `56px minmax(0, 1fr)`

**修改位置**:
- `ContextPanel.tsx` — `ProductCardView` 组件重构
- `styles.css` — `.product-card` 和 `.thumb` 样式更新

### 4. 侧边栏优化

**对话列表分组**:
- 按时间分组: 「今天」「昨天」「更早」
- 每组有分组标题 `<h3>`（小号大写灰色文字，与现有 `.section-title h2` 样式一致）

**删除按钮动画**:
- 当前: `opacity: 0 → 1`（hover 时显示）
- 改为: `transform: translateX(100%) → translateX(0)`（hover 时从右侧滑入）
- 增加过渡: `transition: transform var(--duration-med) var(--ease-out)`

**修改位置**:
- `Sidebar.tsx` — 对话列表分组逻辑
- `styles.css` — `.conversation-delete` 样式更新，添加分组标题样式

### 5. 整体布局

**三栏比例调整**:
- 从 `264px minmax(0, 1fr) 380px` → `256px minmax(0, 1fr) 360px`
- 右侧面板添加折叠/展开按钮（`<PanelRightClose />` / `<PanelRightOpen />` lucide icon）
- 折叠时隐藏右侧面板，聊天区自动填满

**聊天气泡样式适配 MD**:
- assistant 气泡去掉 `white-space: pre-wrap`（MD 自带换行）
- assistant 气泡添加 `.markdown-content` 类容器
- 用户气泡保持 `white-space: pre-wrap`

**移动端抽屉** (`<820px`):
- 右侧面板不再占据网格行
- 改为底部标签切换: 「聊天」「商品」「详情」
- 聊天标签为默认，点击「商品」标签从底部滑出产品/依据面板
- 标签栏使用 `position: sticky; bottom: 0`
- 标签切换时内容区有 `slide-up` 动画

**修改位置**:
- `styles.css` — `.shell` grid 比例、折叠按钮、MD 气泡、移动端抽屉
- `App.tsx` — 添加 `contextPanelCollapsed` state，移动端 `activeMobileTab` state
- `ChatPanel.tsx` — 传入折叠状态
- `ContextPanel.tsx` — 适配折叠/抽屉模式

### 6. 图片策略路线图

| 阶段 | 内容 | 改动 |
|------|------|------|
| 短期（本次） | `image_url` 为 null 时渲染类别 icon + 渐变占位符 | 仅前端 |
| 中期 | 接入真实商品图片源，更新数据库 `sku.image_url` | 数据管道 + DB 更新 |
| 远期 | 图片 CDN + 懒加载 + 缩略图生成 | 基础设施 |

---

## 文件变更清单

### 新建文件
| 文件 | 用途 |
|------|------|
| `frontend/src/components/MarkdownContent.tsx` | MD 渲染组件 |
| `frontend/src/components/ProductInlineCard.tsx` | 聊天内产品卡片 |
| `frontend/src/components/markdown.css` | MD 渲染样式 |
| `frontend/src/components/MobileTabBar.tsx` | 移动端底部标签栏 |

### 修改文件
| 文件 | 改动 |
|------|------|
| `frontend/src/types.ts` | `ChatMessage` 添加 `products` 字段 |
| `frontend/src/App.tsx` | SSE 事件处理关联 products 到消息；添加面板折叠/移动端标签状态 |
| `frontend/src/components/ChatPanel.tsx` | `MessageRow` 使用 `MarkdownContent`；渲染产品卡片行 |
| `frontend/src/components/ContextPanel.tsx` | 产品卡片缩略图改大；图片占位符；折叠/抽屉适配 |
| `frontend/src/components/Sidebar.tsx` | 对话列表时间分组；删除按钮滑入动画 |
| `frontend/src/styles.css` | MD 气泡样式；三栏比例；折叠按钮；移动端抽屉；产品卡片样式 |
| `frontend/package.json` | 添加 `react-markdown`, `remark-gfm` 依赖 |

### 不修改
| 文件 | 原因 |
|------|------|
| 后端代码 | 不需要修改，SSE 已推送完整 products 数据 |
| `frontend/src/api.ts` | 不需要修改，SSE 解析逻辑不变 |

---

## 技术风险

| 风险 | 缓解 |
|------|------|
| 流式 MD 渲染性能 | react-markdown 每帧重渲染可接受；必要时可节流到 50ms 间隔 |
| 产品匹配时机 | context 事件在 delta 之前，产品数据可用后才渲染卡片 |
| 移动端抽屉复杂度 | 使用 CSS `transform` + `transition`，不引入额外动画库 |
| 图片占位符类别推断 | 使用 `category` 字段做简单映射，未知类别 fallback 到 Package icon |