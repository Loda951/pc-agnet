---
title: PC Agent 项目主线文档
description: "汇总 PC 外设客服 Agent 的项目定位、已完成 MVP、架构、遗留债务、Post-MVP 方向和第二阶段主线指引。"
tags: [pc-agent, 主线, 架构, MVP, 第二阶段, 鉴权, RAG, evidence, 前端工作台]
category: 主线
doc_type: context
stage: phase-2
status: active
priority: P0
---

# PC Agent 项目主线文档

## 项目定位与核心价值主张

- 本项目是面向 PC 外设电商场景的客服 AI Agent，核心价值是用结构化商品、订单、物流、售后政策和知识数据，回答高频低风险客服问题。
- 当前 MVP 已从“需求设想”推进到“demo 技术闭环”：本地 Podman 基础设施、PostgreSQL 数据模型、FastAPI API、LangGraph Agent、DeepSeek 调用、React 工作台均已跑通。
- `design.md` 对当前阶段的产品边界定义更保守：定位为 read-only 智能问答客服，不应执行真实业务写操作；当前 MVP 为验证闭环已经实现 demo 售后工单创建，这是一个需要后续收敛的范围差异。
- 项目应优先服务三类价值：降低商品咨询和订单履约查询的人力负担，保证回答基于可追溯业务数据，明确阻断越权、写操作假执行和高风险售后结论。

## 已完成MVP功能清单

### 本地基础设施

- 已完成：通过 `scripts/podman-infra.sh` 使用原生 Podman 启动 PostgreSQL、Redis、ChromaDB。
- 已完成：`/api/health` 可检查 PostgreSQL、Redis、ChromaDB，Chroma 优先使用 `/api/v2/heartbeat`。
- 已完成：`compose.yml` 保留为 Compose 编排参考，但默认不依赖 `podman compose`。

### 后端 API

- 已完成：`/api/chat` 和 `/api/chat/stream`，由 `AgentRuntime` 驱动 LangGraph 流程。
- 已完成：`/api/catalog/search`，支持商品关键词、分类、价格、规格过滤。
- 已完成：`/api/orders/latest` 和 `/api/orders/{order_id}`，按用户 ID 查询订单与物流。
- 已完成：`/api/after-sales`，可创建 demo 售后工单。
- 部分完成：API 使用 `DEFAULT_USER_ID` 和可选 `user_id` 查询参数，没有真实鉴权。

### Agent 能力

- 已完成：基于规则的意图识别，覆盖 `product_recommendation`、`order_status`、`after_sales`、`general`。
- 已完成：商品推荐、最近订单查询、售后意图回复可通过 DeepSeek 生成中文回答。
- 已完成：会话、消息、agent run、工具调用和简单长期记忆持久化。
- 已完成：知识库 RAG 基础闭环，`knowledge_document` 可同步到 ChromaDB，售后政策、FAQ、店铺规则和外设知识回答可返回 evidence。
- 部分完成：多轮上下文依赖 `conversation_id` 和有限偏好记忆，没有完整指代消解。
- 已完成：`design.md` 要求的 `in_scope_auto` / `human_handoff_required` / `out_of_scope` 三态边界分类基础版本。

### 数据与种子

- 已完成：Alembic 初始 PostgreSQL schema，覆盖用户、商品 EAV、SKU/SPU、订单、物流、会话、工具调用、记忆、知识文档、售后表。
- 已完成：`scripts.seed_demo` 导入 demo 用户、5 个 SKU、1 个订单、覆盖 policy/FAQ/store_rule/peripheral_knowledge 的知识文档。
- 已完成：`import_pc_part_dataset.py` 和 `dataset_mapper.py` 可将 `docyx/pc-part-dataset` JSON/JSONL 映射到本地商品模型。
- 已完成：真实 mouse、keyboard、headphones 目录导入路径已打通，核心外设属性可用于筛选、排序、对比和兼容性解释。

### 前端工作台

- 已完成：React/Vite 单页客服工作台，包含聊天区、商品结果区、订单上下文区、售后工单区。
- 已完成：快捷 prompt、消息输入、商品卡、订单卡、售后创建表单。
- 部分完成：前端 build 通过；浏览器中完整手点 MVP 主流程仍需人工验证。

### 验证

- 已完成：Podman 服务启动、数据库迁移、demo seed、`/api/health`、真实 DeepSeek `/api/chat`、订单查询、售后创建均已验证通过。
- 已完成：`pytest backend/tests` 通过，当前包含配置解析、数据集映射、商品搜索、RAG、边界分类和 API 集成测试。
- 已完成：`ruff check backend` 和 `npm run build` 通过。
- 已完成：补充覆盖 `/api/chat`、`/api/orders/latest`、`/api/after-sales` 的数据库集成测试，LLM 与知识检索外部依赖在测试中隔离。

## 技术架构概览

### 技术栈

- 后端：Python 3.11+、FastAPI、LangGraph、LangChain、LangChain OpenAI-compatible client、SQLAlchemy asyncio、Pydantic v2、Alembic、asyncpg、httpx、redis。
- 前端：React 19、TypeScript、Vite、lucide-react。
- 数据与基础设施：PostgreSQL、Redis、ChromaDB、Podman、本地 `.env` 配置。
- LLM：默认 DeepSeek，`LLM_BASE_URL` 留空时由 `backend/app/core/llm.py` 解析为 `https://api.deepseek.com`；仍保留 Qwen 分支。

### 目录结构

- `backend/app/main.py`：FastAPI 应用入口，挂载 health、chat、catalog、orders、after-sales 路由。
- `backend/app/api/routers/`：HTTP API 层，负责请求/响应和依赖注入。
- `backend/app/agent/`：Agent 状态、意图识别、系统提示词、LangGraph 状态机。
- `backend/app/repositories/`：数据库读取与写入封装，包括商品、订单、售后、会话记忆。
- `backend/app/models/`：SQLAlchemy ORM 模型，按 commerce、conversation、support 拆分。
- `backend/app/schemas/`：Pydantic 请求与响应模型。
- `backend/scripts/`：demo seed 和外部商品数据集导入脚本。
- `frontend/src/`：工作台 UI、API 调用和前端类型定义。
- `scripts/podman-infra.sh`：原生 Podman 本地服务管理脚本。
- `docs/`：阶段上下文和本主线文档。

### 关键依赖和配置

- `.env.example`：展示本地数据库、Redis、Chroma、CORS、DeepSeek/Qwen 配置。
- `.env`：本地真实配置文件，包含 DeepSeek key；必须保持 git ignored。
- `backend/pyproject.toml`：后端依赖、pytest、Ruff 规则；Ruff 行宽 100，启用 `E/F/I/UP/B`。
- `frontend/package.json`：前端 dev/build/preview 命令。

### 数据流

- 前端调用 `frontend/src/api.ts`，默认同源请求 `/api`，开发环境由 Vite proxy 转发到 `http://127.0.0.1:8000`。
- `/api/chat` 接收用户问题后进入 `AgentRuntime`。
- LangGraph 流程为 `load_context -> classify_boundary -> route_intent -> retrieve -> retrieve_knowledge -> generate -> persist`。
- `classify_boundary` 先判断 read-only 边界；`route_intent` 使用规则识别业务意图；`retrieve` 根据意图调用商品或订单 repository。
- `retrieve_knowledge` 将 PostgreSQL `knowledge_document` 同步到 ChromaDB 并返回 evidence。
- `generate` 在有 LLM key 时调用 DeepSeek，否则使用后端 fallback 文案；知识类回答会携带依据。
- `persist` 写入用户消息、助手消息、agent run、工具调用和简单记忆。
- 售后办理当前不由 Agent 自动执行；前端会通过聊天入口触发人工接管提示，`/api/after-sales` 保留但降级为 `409 human_handoff_required`。

## 已知问题与遗留债务

- 产品边界债务：售后写操作已降级为人工接管入口，但真实人工队列或工单流转尚未接入。
- 边界分类生产化债务：已实现 `in_scope_auto`、`human_handoff_required`、`out_of_scope` 规则版分类，后续需要评测集和更多可观测指标。
- 鉴权基础版已完成：登录、刷新、登出、当前用户依赖和订单/会话/记忆/售后记录隔离已落地；后续仍需生产级身份源、密码重置、账号管理和审计能力。
- RAG 生产化债务：当前使用本地 deterministic hash embedding 支撑 demo 和测试，后续可接入生产级 embedding provider，并改造为增量同步。
- Evidence 范围债务：当前 evidence 主要覆盖知识文档；商品、订单、物流事实尚未统一纳入 evidence schema。
- 多轮能力有限：当前只保存会话和简单偏好记忆，缺少稳健指代消解、上一款/这个商品承接、多子任务拆分。
- 数据质量有限：真实外设导入路径已打通，但本地环境仍需按需执行导入脚本；搜索排序还缺离线评测集。
- 测试覆盖不足：已补 API 集成、边界分类和 RAG 回归测试；仍缺真实鉴权、权限隔离和多轮指代消解测试。
- 错误处理较薄：LLM 超时、DeepSeek 错误、数据库异常、外部服务降级尚未形成统一错误码和用户可读策略。
- Frontend 仍是 demo 工作台：单用户、无登录、无路由、缺少完整 loading/error/retry 体验和浏览器端验收记录。

## 设计目标与当前实现的差距分析（对照 design.md）

### 产品范围

- 设计目标：当前阶段只做 PC 外设电商 read-only 智能问答。
- 当前实现：商品和订单查询基本符合 read-only；售后办理类请求已降级为人工接管提示。
- 差距判断：当前仍缺真实人工客服队列、鉴权与更完整的权限隔离。

### Must Have 能力

- 商品咨询、推荐、价格、库存问答：部分实现，基于 SKU/SPU/属性检索和 LLM 生成；对比和兼容性问答仍较弱。
- 订单状态、订单内容、物流查询：已实现 demo 路径，能读取最新订单和指定订单。
- 售后政策与流程说明：基础实现，demo 知识文档已接入 RAG；办理类售后请求仍按边界分类转人工。
- FAQ 与店铺知识问答：基础实现，demo 文档可通过 ChromaDB 检索并返回 evidence。
- 多轮上下文承接：部分实现，支持 conversation_id 和偏好记忆；缺少完整指代消解。
- 信息不足时澄清：部分实现，fallback 和 LLM 可能追问，但没有显式澄清状态和规则。
- 三态边界分类：基础实现，当前为规则分类。

### Should Have 能力

- 一轮多子任务拆分：未实现专门规划逻辑。
- 图片、PDF、docx 辅助信息提取：未实现。
- 关键事实 evidence 约束：基础实现，知识文档回答可显式输出 evidence；订单和商品事实 evidence 尚未统一化。
- mixed-intent 分段回复：依赖 LLM 自然生成，没有结构化保障。

### Safety 和质量要求

- 设计目标：不假装执行真实操作，不承诺退款/赔付/责任结论，不暴露无权限订单信息。
- 当前实现：系统提示词限制编造事实，售后办理写操作已降级为人工接管；订单权限仍仅靠默认用户。
- 差距判断：需要在正式扩展前补真实鉴权、权限隔离和人工队列/工单流转。

## 代码约定与开发规范

- Python 使用 4 空格缩进，公共函数尽量保留类型标注。
- 后端遵循 router / schema / repository / service / agent 分层；router 保持薄层，数据库访问放 repository，业务编排放 service 或 agent。
- 新增数据库结构不要修改已合入 migration；应新增 Alembic migration。
- 后端提交前运行 `cd backend && pytest && ruff check .`。
- 前端提交前运行 `cd frontend && npm run build`。
- 本地容器统一使用 `./scripts/podman-infra.sh`，不要新增 Docker 专属命令，也不要默认依赖 `podman compose`。
- 不提交 `.env`、`.env.*`、API key、数据库密码或真实用户数据。
- DeepSeek key 只放本地 `.env` 的 `LLM_API_KEY`；`LLM_BASE_URL` 默认留空即可。
- React 组件使用 PascalCase，变量、hook、状态名使用 camelCase。
- 测试文件命名 `test_*.py`，测试函数命名 `test_*`。

## 第一阶段主线指引（已完成）

### P0：收敛 read-only 边界与人工接管策略

- 目标：实现 `in_scope_auto` / `human_handoff_required` / `out_of_scope` 统一边界分类，并决定售后创建是降级为人工接管入口还是升级为正式可写 workflow。
- 对核心价值的影响：直接决定系统可信度和安全边界，是从 demo 走向业务可用的前置条件。
- 技术复杂度评估：中等；需要新增边界分类模块、响应 schema、Agent 路由分支、前端展示和测试。
- 与现有架构的衔接方式：扩展 `backend/app/agent/intent.py` 或新增 boundary classifier，在 `route_intent` 前后加入边界节点；前端根据分类显示自动回答、人工接管或拒答说明。

### P0：接入知识库 RAG 与 evidence 输出

- 状态：基础版本已完成，详见 `docs/feature/接入知识库 RAG 与 evidence 输出.md`。
- 目标：把 `knowledge_document` 写入 ChromaDB，新增检索节点，让售后政策、FAQ、店铺规则和外设知识回答带可追溯依据。
- 对核心价值的影响：显著提升事实可信度，补齐 `design.md` 中政策/FAQ/知识问答和 evidence 质量要求。
- 技术复杂度评估：中等偏高；需要 embeddings/provider 决策、Chroma collection 管理、检索结果 schema、prompt 注入和回归测试。
- 与现有架构的衔接方式：在 LangGraph 的 `retrieve` 与 `generate` 之间加入 `retrieve_knowledge` 节点，复用 PostgreSQL `knowledge_document` 作为元数据源，Chroma 保存向量索引。

### P1：导入真实商品数据并强化推荐/对比/兼容性

- 状态：基础版本已完成，详见 `docs/feature/导入真实商品数据并强化推荐与集成测试.md`。
- 目标：从 `docyx/pc-part-dataset` 导入 mouse、keyboard、headphones 等真实数据，增强筛选、排序、对比和兼容性解释。
- 对核心价值的影响：直接改善商品推荐质量，减少 demo 数据过少导致的空结果。
- 技术复杂度评估：中等；mapper 已有基础，但需要数据清洗、分类属性规范、搜索排序和验收样例。
- 与现有架构的衔接方式：已扩展 `dataset_mapper.py`、`import_pc_part_dataset.py` 和 `CatalogRepository`，暂未新增属性标准化表。

### P1：补齐集成测试与可回归验收集

- 状态：基础版本已完成，详见 `docs/feature/导入真实商品数据并强化推荐与集成测试.md`。
- 目标：覆盖 `/api/chat`、`/api/orders`、`/api/after-sales`、边界分类、RAG 检索和关键 prompt 行为。
- 对核心价值的影响：降低后续改 Agent 和 schema 时的回归风险。
- 技术复杂度评估：中等；需要测试数据库策略、LLM mock、固定 seed fixture 和 API client。
- 与现有架构的衔接方式：已在 `backend/tests/` 新增 FastAPI async client 测试，LLM 通过空 key/fake service 隔离，PostgreSQL 通过事务 fixture 回滚。

### P2：前端工作台产品化

- 目标：把 demo UI 打磨为可操作的客服工作台，补登录态占位、错误重试、证据展示、人工接管提示和多轮上下文可视化。
- 对核心价值的影响：提升验收和演示质量，但依赖后端边界、RAG、数据质量先稳定。
- 技术复杂度评估：中等；主要是状态管理、交互设计和 API 契约调整。
- 与现有架构的衔接方式：在 `frontend/src/App.tsx` 拆分组件，引入更明确的响应状态和 evidence/boundary 字段展示。

### 最优先推荐

- 第一优先：前端工作台产品化。理由是后端边界、RAG、真实商品导入和核心 API 回归网已具备基础，下一步需要把 evidence、人工接管、错误重试和多轮上下文做成可演示体验。
- 第二优先：鉴权与权限隔离。理由是订单和售后仍依赖 demo 用户，进入更真实场景前需要避免越权查询和敏感信息泄露。

## 第二阶段主线指引：从单用户 demo 到多用户可信工作台

## 阶段判断

- 第一阶段已完成“技术闭环”和三项关键基础优化：read-only 边界与人工接管提示、知识库 RAG 与 evidence、真实外设数据导入与集成测试。
- 第二阶段不应优先扩大自动写操作范围，而应把当前 demo 推进到“多用户可用、可追溯、可演示、可安全扩展”的客服工作台。
- 当前最大风险不是推荐能力不足，而是用户身份仍由 `DEFAULT_USER_ID` 或 query `user_id` 决定；订单、会话、记忆和售后上下文在真实多用户场景下缺少可信隔离。
- 第二阶段推荐的最小可信闭环是：真实登录态 -> 当前用户隔离 -> 流式回复 -> 商品/订单/知识依据统一展示 -> 人工接管形成真实队列或记录 -> 回归测试覆盖越权、SSE 和多轮上下文。

## 对照 feature 文档后的未完成/部分完成项优先级

1. P0：真实鉴权与权限隔离。`docs/feature/收敛 read-only 边界与人工接管策略.md` 已把订单 query `user_id` 标为遗留风险，必须先补。
2. P0：人工接管从“提示”升级为“可追踪队列”。当前 `human_handoff_required` 只改变回答和前端状态，尚未形成客服可处理记录。
3. P0：前端 SSE 真流式输出与状态体验。`/api/chat/stream` 目前是在完整回答生成后按行发送，并非 token/progress 级流式。
4. P1：工作记忆与个性化记忆分层。当前只有简单 `MemoryFact`，缺少上一款商品、当前筛选条件、最近订单等会话级工作记忆。
5. P1：商品、订单、物流事实统一 evidence。RAG evidence 已覆盖知识文档，但商品推荐和订单查询仍没有统一来源结构。
6. P1：外部图片源接入。`sku.image_url` 字段已存在，但真实导入和前端展示尚未建立图片来源、许可、缓存和降级策略。
7. P1：推荐、对比、兼容性继续增强。真实数据导入基础完成，但搜索排序仍是轻量规则，缺少离线评测集、对比结构化输出和指代承接。
8. P2：边界分类生产化。规则分类已可用，后续需要评测集、误判样例、可观测指标和可选模型分类器。
9. P2：MCP 工具试点。当前内部 repository 工具足够支撑核心链路，MCP 更适合作为外部只读能力和人工系统集成的扩展层。

## 第二阶段优化点清单

### P0：多用户鉴权、会话管理与用户隔离

- 状态：基础版本已完成，详见 `docs/feature/多用户鉴权、会话管理与用户隔离.md`。
- 所属维度：多用户鉴权与记忆系统。
- 简明描述：把系统从 demo 默认用户切换为真实登录用户，所有订单、会话、记忆和人工接管记录都以当前认证用户为唯一可信身份来源。
- 需要实现的功能点：新增认证相关 migration，例如 `user_auth_credential`、`user_session` 或 refresh token 表；为 `AppUser` 补充唯一登录标识、状态、最近登录时间等字段；实现 `AuthService`、密码哈希、登录、刷新、登出和 `get_current_user` FastAPI 依赖；`/api/chat`、`/api/orders/*`、后续人工接管接口全部移除公开 `user_id` 参数；`ChatRequest.user_id` 仅可在受控 dev/test 路径保留或彻底删除；前端增加登录页、会话恢复、401/403 处理和退出登录；测试覆盖用户 A 不能读取用户 B 的订单、会话、记忆和人工接管记录。

## P0：人工接管队列与 read-only 安全闭环

- 所属维度：功能完善。
- 简明描述：保持第二阶段默认 read-only，但把 `human_handoff_required` 从纯提示升级为真实可追踪的人工接管请求，避免用户以为系统已经办理退款、退货或维修。
- 需要实现的功能点：新增 `handoff_request` 或扩展售后表作为人工队列入口，保存用户、会话、订单、原因、边界分类、状态和处理备注；`/api/after-sales` 从固定 409 逐步改为创建人工接管记录并返回 `202 accepted` 或明确的队列响应；Agent 建议动作中的“转人工客服”调用该入口；前端展示接管状态、请求时间、关联订单和可取消/补充说明；保留 read-only 策略，不自动承诺退款、赔付、维修结论或订单修改；测试覆盖办理类请求不进入自动业务写操作。

## P0：AI 回复 SSE 真流式输出

- 所属维度：优化前端展示。
- 简明描述：把聊天体验从“等待完整回答”改为可见的事件流，让用户先看到边界判断、检索进度、依据更新和逐字回答。
- 需要实现的功能点：后端新增 `AgentRuntime.run_stream()` 或等价流式接口，事件类型建议包含 `run_started`、`boundary`、`tool_call`、`context`、`delta`、`done`、`error`；LLM provider 支持 token streaming，fallback 回答也按 chunk 输出；`/api/chat/stream` 使用 POST + `StreamingResponse`，避免等待 `run()` 完整结束后再按行拆分；前端新增 `sendChatStream`，用 fetch reader 解析 SSE，因为 EventSource 不适合携带 POST body；消息气泡边生成边渲染，右侧商品、订单、evidence 面板在 `context` 事件到达时更新；断流、超时、取消和重试都要有用户可理解的状态。

## P1：工作记忆与长期个性化记忆分层

- 所属维度：多用户鉴权与记忆系统。
- 简明描述：需要引入工作记忆。现有长期偏好记忆只能表达“偏好无线设备”这类稳定事实，无法可靠解决“这款”“上一单”“刚才那个无线款”等多轮指代问题。
- 需要实现的功能点：新增会话级 `working_memory` 结构或表，按 `conversation_id + user_id` 保存最近商品候选、当前筛选条件、最近订单 ID、未解决槽位、人工接管状态、最近 evidence ID 和短摘要；长期 `MemoryFact` 增加 `scope`、`fact_type`、`source_message_id`、`expires_at`、`last_used_at`、`disabled_at` 等字段，区分偏好、禁忌、设备生态和临时意图；实现 `MemoryService` 作为深模块，接口负责读取工作记忆、合并长期记忆、更新摘要和写入新事实；敏感数据如手机号、地址、完整物流单号不写入长期记忆；前端可展示“当前会话上下文”和“已记住偏好”的可撤销列表；测试通过 Agent 外部接口验证指代消解，不测试内部记忆实现细节。

## P1：商品、订单、物流与知识统一 evidence

- 所属维度：功能完善。
- 简明描述：把 evidence 从知识库专属能力扩展为所有关键事实的统一来源结构，让推荐理由、价格库存、订单状态和物流节点都可追溯。
- 需要实现的功能点：扩展 `EvidenceItem.source_type`，支持 `knowledge_document`、`product`、`sku`、`order`、`order_logistics`；Catalog 和 Order repository 返回结果时同步生成 evidence；LLM prompt 要求涉及价格、库存、订单状态、物流和政策时优先引用 evidence；前端依据面板按来源类型分组展示；`agent_run.state_json` 和消息 metadata 保存统一 evidence；测试覆盖商品推荐、订单查询、售后政策三类回答均包含来源。

## P1：外部商品图片源接入

- 所属维度：引入外部图片。
- 简明描述：优先为 mouse、keyboard、headphones 三类核心外设补充首图，服务商品卡、对比视图和推荐可信度；图片接入要先做来源治理，不能默认热链或抓取不可复用图片。
- 需要实现的功能点：图片来源优先级为真实数据集自带图片 URL、品牌或厂商公开媒体资源、人工维护白名单，再到本地占位图；复用现有 `sku.image_url`，新增或预留 `image_source_url`、`image_source_type`、`image_license_status`、`image_checked_at`、`image_cache_path` 等元数据；导入脚本增加图片字段映射和来源记录；后端如提供图片代理或缩略图缓存，必须做域名 allowlist、content-type 校验、大小限制和 SSRF 防护；本地缓存目录应 git ignored，生产可迁移到对象存储或 CDN；前端商品卡展示图片、无图占位、加载失败降级和图片来源标记。

## P1：前端工作台布局与操作效率优化

- 所属维度：优化前端展示。
- 简明描述：在现有三栏工作台基础上提高信息密度和可操作性，让客服能快速看懂边界、依据、商品候选、订单上下文和人工接管状态。
- 需要实现的功能点：聊天区支持流式消息、取消生成、重试、复制回答和建议动作；商品区支持图片、关键规格高亮、库存/价格状态、对比勾选和按预算/连接方式快捷过滤；订单区隐藏敏感收件信息，展示状态、物流和明细摘要；依据区按知识/商品/订单分组并可折叠；人工接管区展示队列状态和补充说明入口；移动端或窄屏下改为标签页布局，避免三栏挤压；所有错误态、空态、loading 态和断流态都需要可回归验收。

## P1：推荐、对比、兼容性与澄清机制增强

- 所属维度：功能完善。
- 简明描述：在真实数据基础上把推荐从“能搜到”推进到“能解释、能对比、能追问”，尤其覆盖外设购买常见决策点。
- 需要实现的功能点：建立小型离线评测集，覆盖无线鼠标、红轴键盘、带麦耳机、预算约束、品牌偏好和对比问题；商品搜索返回结构化排序原因；Agent 对多商品对比输出统一字段，例如连接方式、价格、库存、关键规格、适合场景和注意事项；信息不足时进入澄清状态，明确追问预算、用途、平台、连接方式或尺寸；工作记忆保存上轮候选，支持“第二个”“这款换无线”“和 G502 比”这类承接。

## P1：RAG 生产化与知识同步增量化

- 所属维度：功能完善。
- 简明描述：保留本地 hash embedding 作为测试和离线 demo adapter，同时增加生产 embedding adapter 和增量同步，避免知识量上升后每次全量 upsert。
- 需要实现的功能点：为 embedding provider 定义小接口，提供本地 hash adapter 和外部 embedding adapter；配置 embedding model、base URL、超时和批量大小，不能让测试依赖真实 key；`KnowledgeDocument` 增加 `updated_at`、内容 hash 或同步版本；`sync_knowledge` 支持按更新时间或 hash 增量同步；Chroma 异常继续降级但要记录可观测错误；补充政策变更、FAQ 命中、低分过滤和 provider 失败测试。

## P2：边界分类评测、观测与可选模型分类器

- 所属维度：功能完善。
- 简明描述：规则分类已适合当前 demo，但进入真实客服语料后要能发现误判，尤其是把写操作误判为自动回答的风险。
- 需要实现的功能点：整理边界分类评测集，覆盖商品咨询、订单查询、政策说明、售后办理、退款承诺、订单修改、越界闲聊和混合意图；记录每次分类的规则命中、分类结果、置信度和人工修正结果；增加边界分类指标面板或日志报表；必要时引入 LLM/小模型分类器作为二级判断，但写操作召回优先于自动回答覆盖率；所有分类器输出仍落到三态 schema。

## P2：MCP 工具增强试点

- 所属维度：工作记忆与 MCP 工具增强。
- 简明描述：需要考虑 MCP，但不建议第二阶段一开始就把内部数据库工具 MCP 化。当前 Catalog、Order、Knowledge repository 是进程内深模块，直接测试和维护成本更低；MCP 更适合作为外部系统和只读增强工具的 adapter。
- 需要实现的功能点：先定义 `ToolRegistry` 或工具调用接口，保持 Agent 只依赖少量工具 schema；MCP 试点范围建议选择外部图片/商品资料查询、厂商知识文档检索、物流只读查询或客服工单系统；所有 MCP tool 默认 read-only，设置 allowlist、超时、重试、审计日志和结果 schema；MCP 调用结果写入 `tool_call`，必要时转为统一 evidence；测试使用 in-memory/fake adapter，不依赖真实外部 MCP 服务；只有当同一能力存在生产 adapter 和测试 adapter 时再把 seam 公开。

### 第二阶段推荐实施顺序

1. 先做多用户鉴权与隔离，同时补越权测试。这是所有订单、记忆和人工接管能力的安全前置。
2. 接着做人工接管队列和 SSE 真流式输出，形成可演示且不假装办理业务的客服体验。
3. 然后做工作记忆、统一 evidence 和前端布局优化，让多轮推荐、订单上下文和依据展示真正可用。
4. 再做外部图片源、推荐对比增强和 RAG 生产化，提高商品咨询质量和视觉可信度。
5. 最后再评估 MCP 试点和模型化边界分类，避免过早引入工具平台复杂度。
