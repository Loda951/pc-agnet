---
title: 收敛 read-only 边界与人工接管策略
description: "记录 read-only 智能客服边界收敛方案，包括三态边界分类、售后写操作降级、前端三态展示、本地代理修复、测试验证和遗留事项。"
tags: [feature, read-only, 边界分类, 人工接管, 售后, 安全策略, LangGraph, 前端展示]
category: feature
doc_type: feature-summary
stage: phase-1
status: completed
priority: P0
---

# 收敛 read-only 边界与人工接管策略

## 背景与目标

- 当前 MVP 已跑通商品推荐、订单查询、售后工单 demo 创建等链路。
- 但产品定位要求当前阶段是 read-only 智能客服，不应自动执行真实业务写操作。
- 本 feature 的目标：
  - 建立统一边界分类：`in_scope_auto` / `human_handoff_required` / `out_of_scope`。
  - 在 Agent 路由前先判断边界，只有低风险 read-only 请求可自动处理。
  - 将售后创建场景降级为人工接管入口。
  - 前端按分类渲染自动回答、人工接管、拒答三种状态。

## 关键变更清单

### 后端 Agent 与 Schema

- `backend/app/schemas/chat.py`
  - 新增 `BoundaryClassification`。
  - `ChatResponse` 新增 `boundary` 字段。
  - 分类值限定为：
    - `in_scope_auto`
    - `human_handoff_required`
    - `out_of_scope`

- `backend/app/agent/intent.py`
  - 新增 `classify_boundary(message)`。
  - 新增边界规则：
    - 商品推荐、订单物流查询、售后政策说明 -> `in_scope_auto`
    - 售后申请、创建工单、退款/维修办理、订单取消/改地址等写操作 -> `human_handoff_required`
    - 非 PC 外设商城客服范围问题 -> `out_of_scope`
  - 保留原有 `classify_intent`，用于通过边界后的业务意图识别。

- `backend/app/agent/state.py`
  - `AgentState` 新增 `boundary` 字段。

- `backend/app/agent/graph.py`
  - LangGraph 流程从：

    ```text
    load_context -> route_intent -> retrieve -> generate -> persist
    ```

    调整为：

    ```text
    load_context -> classify_boundary -> route_intent/retrieve 或直接 generate -> persist
    ```

  - `in_scope_auto` 继续进入 `route_intent`、`retrieve`、`generate`。
  - `human_handoff_required` 和 `out_of_scope` 不再进入检索或自动业务流程，直接生成边界答复。
  - 持久化助手消息时写入 `boundary` 元数据。
  - 只有 `in_scope_auto` 请求才更新用户偏好记忆。
  - 建议动作调整：
    - 人工接管请求返回 `转人工客服`
    - 越界请求返回 `咨询外设推荐`
    - 订单查询后的售后动作改为 `转人工处理售后`

- `backend/app/agent/prompts.py`
  - 系统提示词更新为 read-only 策略。
  - 明确禁止承诺或假装执行退款、退换货、维修、订单修改等写操作。

### 售后写操作降级

- `backend/app/api/routers/after_sales.py`
  - `/api/after-sales` 不再创建 demo 售后工单。
  - 直接返回 `409 Conflict`，`detail` 为 `human_handoff_required` 边界分类结果。
  - 该接口保留为后续正式写 workflow 或人工接管入口的兼容位置。

### 前端三态展示

- `frontend/src/types.ts`
  - 新增 `BoundaryClassification` 类型。
  - `ChatResponse` 和 `ChatMessage` 增加 `boundary` 字段。

- `frontend/src/App.tsx`
  - 新增全局 `boundary` 状态。
  - 助手消息增加边界徽标。
  - 右侧上下文区新增“边界”面板。
  - 快捷 prompt 增加：
    - `我要申请退货`
    - `推荐一台手机`
  - 售后区从“创建工单”改为“转人工处理”。
  - 移除前端自动创建售后工单行为。
  - `out_of_scope` 时清空商品和订单上下文，避免旧上下文误导。

- `frontend/src/styles.css`
  - 新增三态样式：
    - 自动回答
    - 人工接管
    - 拒答
  - 新增边界卡片、边界徽标、人工接管提示样式。

### 前端请求与本地代理

- `frontend/src/api.ts`
  - 默认 API base 从 `http://localhost:8000` 改为同源空字符串。
  - 请求路径变为 `/api/chat`。
  - 删除前端 `createAfterSalesTicket` 封装。

- `frontend/vite.config.ts`
  - 新增 Vite dev server 代理：

    ```ts
    "/api": {
      target: "http://127.0.0.1:8000",
      changeOrigin: true
    }
    ```

- `frontend/package.json`
  - `dev` / `build` / `preview` 显式指定 `--config vite.config.ts`。
  - 避免被本地生成且 git ignored 的 `vite.config.js` 抢占配置。

### 测试

- `backend/tests/test_boundary_classification.py`
  - 覆盖边界分类：
    - 商品推荐 -> `in_scope_auto`
    - 订单物流查询 -> `in_scope_auto`
    - 售后政策说明 -> `in_scope_auto`
    - 售后申请 -> `human_handoff_required`
    - 订单取消 -> `human_handoff_required`
    - 写代码、手机推荐等 -> `out_of_scope`
  - 覆盖人工接管答复。
  - 覆盖 `/api/after-sales` 降级为 `409 + human_handoff_required`。

## Bug 记录

### 1. 前端交互显示 `Failed to fetch`

- 问题描述：
  - 现象：点击聊天快捷 prompt 后，前端只显示 `Failed to fetch`。
  - 根因：前端默认直接请求 `http://localhost:8000/api/chat`，当前浏览器环境直连 8000 被拦截，导致请求没有到达后端。

- 解决方法：
  - `frontend/src/api.ts` 默认改为同源 `/api`。
  - `frontend/vite.config.ts` 增加 `/api -> http://127.0.0.1:8000` 代理。

### 2. Vite 代理配置未生效

- 问题描述：
  - 现象：已在 `vite.config.ts` 配置代理后，`/api/health` 仍返回前端 HTML。
  - 根因：本地存在被 git ignore 的 `frontend/vite.config.js`，Vite 优先读取了旧的 JS 配置，未使用新的 `vite.config.ts`。

- 解决方法：
  - `frontend/package.json` 中 `dev` / `build` / `preview` 显式增加 `--config vite.config.ts`。

### 3. 代理打通后 `/api/chat` 返回 500

- 问题描述：
  - 现象：Vite 代理生效后，请求不再 `Failed to fetch`，但 `/api/chat` 返回 500。
  - 根因：8000 端口上仍运行旧后端实例，未加载本次边界分类改动。

- 解决方法：
  - 停止旧后端进程。
  - 使用当前代码重新启动后端服务。

## 方案与决策

- 决策：边界分类先于业务意图识别。
  - 理由：安全边界应优先于业务能力，避免越权请求进入自动检索或 LLM 生成流程。

- 决策：售后办理类请求统一降级为 `human_handoff_required`。
  - 理由：退货、换货、退款、维修等涉及写操作、责任判断或承诺风险，当前阶段不自动执行。

- 决策：售后政策说明仍允许 `in_scope_auto`。
  - 理由：政策、流程、条件说明属于 read-only 问答，可以自动回答；办理动作才转人工。

- 决策：`/api/after-sales` 不删除，而是返回人工接管边界。
  - 理由：保留 API 位置，方便后续升级为正式写 workflow 或人工工单入口。

- 决策：前端默认走同源 `/api`，开发环境由 Vite 代理后端。
  - 理由：减少浏览器跨端口和 CORS 干扰，本地联调更稳定。

- 决策：前端显式使用 `vite.config.ts`。
  - 理由：避免 TypeScript build 产物 `vite.config.js` 干扰 Vite 实际加载的配置。

## 验证结果

- 后端测试：

  ```text
  15 passed
  ```

- 后端 lint：

  ```text
  ruff check . -> All checks passed
  ```

- 前端构建：

  ```text
  npm run build -> passed
  ```

- 浏览器联调：
  - `我要申请退货` -> 人工接管
  - `推荐一台手机` -> 拒答
  - `帮我查最近订单` -> 自动回答

## 遗留事项

- `human_handoff_required` 目前只是分类和提示，还没有真实人工客服队列或工单流转。
- 售后政策回答仍依赖现有 prompt 和有限上下文，后续应接入知识库 RAG 与 evidence。
- 订单查询仍依赖 demo 用户和默认 `user_id`，真实鉴权与权限隔离尚未完成。
- 边界分类当前为规则实现，后续可结合评测集和模型分类器增强召回与准确率。
- `AGENTS.md` 在本 feature 开始前已有未提交修改，本次 feature 未将其纳入实现范围。
