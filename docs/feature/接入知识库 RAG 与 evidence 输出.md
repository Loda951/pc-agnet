---
title: 接入知识库 RAG 与 evidence 输出
description: "记录知识库 RAG 基础闭环的目标、实现方案、ChromaDB 同步、evidence schema、前端依据展示、测试验证和后续生产化债务。"
tags: [feature, RAG, evidence, 知识库, ChromaDB, embedding, LangGraph, FastAPI, 前端展示]
category: feature
doc_type: feature-summary
stage: phase-1
status: completed
priority: P0
---

# 接入知识库 RAG 与 evidence 输出

## 背景与目标

- PostgreSQL 已有 `knowledge_document` 表，ChromaDB 已在本地基础设施中启动，但此前 Agent 没有知识索引、检索节点或 evidence 输出。
- 本 feature 的目标：
  - 将 `knowledge_document` 同步写入 ChromaDB collection。
  - 在 LangGraph `retrieve` 与 `generate` 之间加入 `retrieve_knowledge` 节点。
  - 让售后政策、FAQ、店铺规则和外设知识回答返回可追溯 `evidence`。
  - 保持测试不依赖真实 LLM API key 或外部 embedding provider。

## 关键变更清单

### 后端 Schema 与配置

- `backend/app/schemas/chat.py`
  - 新增 `EvidenceItem`。
  - `ChatResponse` 新增 `evidence: list[EvidenceItem]`。

- `backend/app/core/config.py`
  - 新增 `KNOWLEDGE_COLLECTION`，默认 `pc_agent_knowledge`。
  - 新增 `KNOWLEDGE_SCORE_THRESHOLD`，默认 `0.16`。

- `.env.example`
  - 同步新增知识库 collection 和检索阈值配置。

### 知识库 Repository 与 Service

- `backend/app/repositories/knowledge.py`
  - 新增 `KnowledgeRepository`。
  - 支持列出知识文档、按 ID 回查、回写 `chroma_collection` 和 `chroma_id`。

- `backend/app/services/knowledge_rag.py`
  - 新增 `LocalHashEmbeddingProvider`：确定性本地 hash embedding，支持离线测试和无 key 本地 demo。
  - 新增 `ChromaKnowledgeService`：
    - `sync()`：将 PostgreSQL `knowledge_document` upsert 到 ChromaDB。
    - `retrieve(query)`：先同步文档，再进行向量检索，并转为 `EvidenceItem`。
  - Chroma metadata 使用扁平 scalar，原始 metadata 仍以 PostgreSQL 为准。

- `backend/scripts/sync_knowledge.py`
  - 新增显式同步脚本：

    ```bash
    cd backend
    python -m scripts.sync_knowledge
    ```

### LangGraph Agent

- `backend/app/agent/graph.py`
  - LangGraph 流程调整为：

    ```text
    load_context -> classify_boundary -> route_intent -> retrieve -> retrieve_knowledge -> generate -> persist
    ```

  - 只有 `in_scope_auto` 请求进入知识检索。
  - `retrieve_knowledge` 会记录 `knowledge.retrieve` tool call。
  - Chroma 检索异常降级为空 evidence，不阻断聊天主流程。
  - LLM context、fallback 文案、消息 metadata 和 `agent_run.state_json` 均携带 evidence。

- `backend/app/agent/prompts.py`
  - 系统提示词要求有 evidence 时输出“依据”，并只基于 evidence 说明政策、FAQ、店铺规则或外设知识。

### Demo 数据

- `backend/scripts/seed_demo.py`
  - 知识文档从两条 policy 扩展为：
    - 七天无理由退货政策
    - 外设保修说明
    - 发票与发货 FAQ
    - 店铺价保规则
    - 机械键盘轴体选购知识
  - Seed 改为按标题补齐缺失文档，重跑不会因为第一条已存在而跳过新文档。

### 前端展示

- `frontend/src/types.ts`
  - 新增 `EvidenceItem` 类型。
  - `ChatResponse` 新增 `evidence` 字段。

- `frontend/src/App.tsx`
  - 新增 evidence 状态。
  - 右侧上下文区新增“依据”面板，展示来源标题、类型、片段、来源 ID 和相似度。

- `frontend/src/styles.css`
  - 新增 evidence 列表和证据卡片样式。

## 方案与决策

- 决策：embedding provider 先使用确定性本地 hash embedding。
  - 理由：当前阶段不能让测试依赖真实 LLM API key 或外部 embedding 服务；本地 provider 足够支撑 demo 知识检索闭环。

- 决策：检索时先懒同步 PostgreSQL 文档到 ChromaDB，同时提供显式同步脚本。
  - 理由：本地 demo 不需要额外后台任务；脚本适合 seed 后初始化，懒同步保证漏跑脚本时也能自愈。

- 决策：evidence 以 PostgreSQL `knowledge_document` 为权威元数据源。
  - 理由：Chroma 只负责向量索引，回答展示的标题、类型、内容片段和 metadata 从业务数据库回查，避免索引 metadata 漂移。

- 决策：Chroma 异常不打断聊天。
  - 理由：知识库是事实增强路径，Chroma 临时不可用时应退化为无 evidence 回答，并在 tool call 中留下错误信息。

## 验证结果

- 新增单测：
  - Chroma upsert 与 evidence 返回。
  - 低相似度结果过滤。
  - 售后 fallback 回答包含知识依据。
  - 本地 hash embedding 稳定性。

## 遗留事项

- 本地 hash embedding 适合 demo 和回归测试，不等同于生产级语义 embedding；后续可增加可配置的外部 embedding provider。
- Chroma 同步目前是全量 upsert，数据量增大后可改为按 `updated_at` 或内容 hash 增量同步。
- Evidence 当前只有知识文档来源；商品、订单、物流也可以在后续统一纳入 evidence schema。
